from __future__ import annotations

import socket
import sys
import time
import typing
from datetime import datetime, timezone
from functools import lru_cache
from socket import SOCK_DGRAM, SOCK_STREAM
from socket import timeout as SocketTimeout

try:  # Compiled with SSL?
    import ssl

    from ..util.ssltransport import SSLTransport
except (ImportError, AttributeError):
    ssl = None  # type: ignore[assignment]
    SSLTransport = None  # type: ignore


try:  # We shouldn't do this, it is private. Only for chain extraction check. We should find another way.
    from _ssl import Certificate  # type: ignore[import-not-found]
except (ImportError, AttributeError):
    Certificate = None

from .._collections import HTTPHeaderDict
from .._constant import (
    DEFAULT_BLOCKSIZE,
    DEFAULT_KEEPALIVE_DELAY,
    UDP_DEFAULT_BLOCKSIZE,
    responses,
)
from ..contrib.hface import (
    HTTP1Protocol,
    HTTP2Protocol,
    HTTP3Protocol,
    HTTPOverQUICProtocol,
    HTTPOverTCPProtocol,
    HTTPProtocolFactory,
    QuicTLSConfig,
)
from ..contrib.hface.events import (
    ConnectionTerminated,
    DataReceived,
    EarlyHeadersReceived,
    Event,
    HandshakeCompleted,
    HeadersReceived,
    StreamResetReceived,
)
from ..exceptions import (
    EarlyResponse,
    IncompleteRead,
    InvalidHeader,
    MustDowngradeError,
    MustRedialError,
    ProtocolError,
    ResponseNotReady,
    SSLError,
)
from ..util import parse_alt_svc, resolve_cert_reqs
from ._base import (
    BaseBackend,
    ConnectionInfo,
    DirectStreamAccess,
    HttpVersion,
    LowLevelResponse,
    QuicPreemptiveCacheType,
    ResponsePromise,
)

if typing.TYPE_CHECKING:
    from .._typing import _TYPE_SOCKET_OPTIONS

_HAS_SYS_AUDIT = hasattr(sys, "audit")


@lru_cache(maxsize=1)
def _HAS_HTTP3_SUPPORT() -> bool:
    from ..util.ssl_ import IS_FIPS

    if IS_FIPS:
        return False

    import importlib.util

    try:
        return importlib.util.find_spec("qh3") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


class HfaceBackend(BaseBackend):
    supported_svn = [HttpVersion.h11, HttpVersion.h2, HttpVersion.h3]

    def __init__(
        self,
        host: str,
        port: int | None = None,
        timeout: int | float | None = -1,
        source_address: tuple[str, int] | None = None,
        blocksize: int = DEFAULT_BLOCKSIZE,
        *,
        socket_options: _TYPE_SOCKET_OPTIONS
        | None = BaseBackend.default_socket_options,
        disabled_svn: set[HttpVersion] | None = None,
        preemptive_quic_cache: QuicPreemptiveCacheType | None = None,
        keepalive_delay: float | int | None = DEFAULT_KEEPALIVE_DELAY,
    ):
        if not _HAS_HTTP3_SUPPORT():
            if disabled_svn is None:
                disabled_svn = set()
            disabled_svn.add(HttpVersion.h3)

        super().__init__(
            host,
            port,
            timeout,
            source_address,
            blocksize,
            socket_options=socket_options,
            disabled_svn=disabled_svn,
            preemptive_quic_cache=preemptive_quic_cache,
            keepalive_delay=keepalive_delay,
        )

        self._proxy_protocol: HTTPOverTCPProtocol | None = None
        self._protocol: HTTPOverQUICProtocol | HTTPOverTCPProtocol | None = None

        self._svn: HttpVersion | None = None

        self._stream_id: int | None = None

        # prep buffer, internal usage only.
        # not suited for HTTPHeaderDict
        self.__headers: list[tuple[bytes, bytes]] = []
        self.__expected_body_length: int | None = None
        self.__remaining_body_length: int | None = None
        self.__authority_bit_set: bool = False
        self.__legacy_host_entry: bytes | None = None
        self.__protocol_bit_set: bool = False

        # h3 specifics
        self.__custom_tls_settings: QuicTLSConfig | None = None
        self.__alt_authority: tuple[str, int] | None = None
        self.__origin_port: int | None = None

        # automatic upgrade shield against errors!
        self._max_tolerable_delay_for_upgrade: float | None = None

    @property
    def is_saturated(self) -> bool:
        if self._protocol is None:
            return False
        # is_available also includes whether we must goaway
        # we want to focus on stream capacity here.
        if self._protocol.is_available() is False:
            return self._protocol.has_expired() is False
        return False

    @property
    def max_stream_count(self) -> int:
        if self._protocol is None:
            return 0
        return self._protocol.max_stream_count

    @property
    def is_multiplexed(self) -> bool:
        return self._protocol is not None and self._protocol.multiplexed

    @property
    def max_frame_size(self) -> int:
        if self._protocol is None:
            return self.blocksize

        try:
            remote_max_size = self._protocol.max_frame_size()
        except NotImplementedError:
            return self.blocksize

        return remote_max_size if self.blocksize > remote_max_size else self.blocksize

    def _new_conn(self) -> socket.socket | None:
        # handle if set up, quic cache capability. thus avoiding first TCP request prior to upgrade.
        if (
            self._svn is None
            and HttpVersion.h3 not in self._disabled_svn
            and self.scheme == "https"
        ):
            if (
                self._preemptive_quic_cache
                and (self.host, self.port) in self._preemptive_quic_cache
            ):
                self.__alt_authority = self._preemptive_quic_cache[
                    (self.host, self.port or 443)
                ]
                if self.__alt_authority:
                    self._svn = HttpVersion.h3
                    # we ignore alt-host as we do not trust cache security
                    self.port: int = self.__alt_authority[1]
            elif (
                HttpVersion.h11 in self._disabled_svn
                and HttpVersion.h2 in self._disabled_svn
            ):
                self.__alt_authority = (self.host, self.port or 443)
                self._svn = HttpVersion.h3
                self.port = self.__alt_authority[1]

        if self._svn == HttpVersion.h3:
            if self.blocksize == DEFAULT_BLOCKSIZE:
                self.blocksize = UDP_DEFAULT_BLOCKSIZE
            self.socket_kind = SOCK_DGRAM

            # undo local memory on whether conn supposedly support quic/h3
            # if conn target another host.
            if self._response and self._response.authority != self.host:
                self._svn = None
                self._response = None  # type: ignore[assignment]
                if self.blocksize == UDP_DEFAULT_BLOCKSIZE:
                    self.blocksize = DEFAULT_BLOCKSIZE
                self.socket_kind = SOCK_STREAM
        else:
            if self.blocksize == UDP_DEFAULT_BLOCKSIZE:
                self.blocksize = DEFAULT_BLOCKSIZE
            self.socket_kind = SOCK_STREAM

        return None

    def _upgrade(self) -> None:
        assert self._response is not None, (
            "attempt to call _upgrade() prior to successful getresponse()"
        )
        assert self.sock is not None
        assert self._svn is not None

        #: Don't search for alt-svc again if already done once.
        if self.__alt_authority is not None:
            return

        #: determine if http/3 support is present in environment
        has_h3_support = _HAS_HTTP3_SUPPORT()

        #: are we on a plain conn? unencrypted?
        is_plain_socket = type(self.sock) is socket.socket

        #: did the user purposely killed h3/h2 support?
        is_h3_disabled = HttpVersion.h3 in self._disabled_svn
        is_h2_disabled = HttpVersion.h2 in self._disabled_svn

        upgradable_svn: HttpVersion | None = None

        if is_plain_socket:
            if is_h2_disabled or self._svn == HttpVersion.h2:
                return
            upgradable_svn = HttpVersion.h2

            self.__alt_authority = self.__altsvc_probe(
                svc="h2c"
            )  # h2c = http2 over cleartext
        else:
            # do not upgrade if not coming from TLS already.

            # already maxed out!
            if self._svn == HttpVersion.h3:
                return

            if is_h3_disabled is False and has_h3_support is True:
                upgradable_svn = HttpVersion.h3
                self.__alt_authority = self.__altsvc_probe(svc="h3")

            # no h3 target found[...] try to locate h2 support if appropriated!
            if not self.__alt_authority and self._svn != HttpVersion.h2:
                upgradable_svn = HttpVersion.h2
                self.__alt_authority = self.__altsvc_probe(svc="h2")

        if self.__alt_authority:
            # we want to infer a "best delay" to wait for silent upgrade.
            # for that we use the previous known delay for handshake or establishment.
            # and apply a "safe" margin of 50%.
            if (
                self.conn_info is not None
                and self.conn_info.established_latency is not None
            ):
                self._max_tolerable_delay_for_upgrade = (
                    self.conn_info.established_latency.total_seconds()
                )
                if self.conn_info.tls_handshake_latency is not None:
                    self._max_tolerable_delay_for_upgrade += (
                        self.conn_info.tls_handshake_latency.total_seconds()
                    )
                self._max_tolerable_delay_for_upgrade *= 10.0
                # we can, in rare case get self._max_tolerable_delay_for_upgrade == 0.0
                # we want to avoid this at all cost.
                if self._max_tolerable_delay_for_upgrade <= 0.01:
                    self._max_tolerable_delay_for_upgrade = 3.0
            else:  # by default (safe/conservative fallback) to 3000ms
                self._max_tolerable_delay_for_upgrade = 3.0

            if upgradable_svn == HttpVersion.h3:
                if self._preemptive_quic_cache is not None:
                    self._preemptive_quic_cache[(self.host, self.port or 443)] = (
                        self.__alt_authority
                    )

                    if (self.host, self.port or 443) not in self._preemptive_quic_cache:
                        return

            self._svn = upgradable_svn
            self.__origin_port = self.port
            # We purposely ignore setting the Hostname. Avoid MITM attack from local cache attack.
            self.port = self.__alt_authority[1]
            self.close()

    def _custom_tls(
        self,
        ssl_context: ssl.SSLContext | None = None,
        ca_certs: str | None = None,
        ca_cert_dir: str | None = None,
        ca_cert_data: None | str | bytes = None,
        ssl_minimum_version: int | None = None,
        ssl_maximum_version: int | None = None,
        cert_file: str | bytes | None = None,
        key_file: str | bytes | None = None,
        key_password: str | bytes | None = None,
        cert_fingerprint: str | None = None,
        assert_hostname: None | str | typing.Literal[False] = None,
        cert_reqs: int | str | None = None,
    ) -> bool:
        """Meant to support TLS over QUIC meanwhile cpython does not ship with its native implementation."""
        if self._svn != HttpVersion.h3:
            return NotImplemented

        cert_use_common_name = False

        allow_insecure: bool = False

        if ssl_context:
            cert_use_common_name = (
                getattr(ssl_context, "hostname_checks_common_name", False) or False
            )

            if ssl_context.verify_mode == ssl.CERT_NONE:
                allow_insecure = True

            if ca_certs is None and ca_cert_dir is None and ca_cert_data is None:
                ctx_root_certificates = ssl_context.get_ca_certs(True)

                if ctx_root_certificates:
                    ca_cert_data = "\n".join(
                        ssl.DER_cert_to_PEM_cert(cert) for cert in ctx_root_certificates
                    )

            if (
                assert_hostname is None
                and hasattr(ssl_context, "check_hostname")
                and ssl_context.check_hostname is False
            ):
                assert_hostname = False

        if not allow_insecure and resolve_cert_reqs(cert_reqs) == ssl.CERT_NONE:
            allow_insecure = True

        self.__custom_tls_settings = QuicTLSConfig(
            insecure=allow_insecure,
            cafile=ca_certs,
            capath=ca_cert_dir,
            cadata=ca_cert_data.encode()
            if isinstance(ca_cert_data, str)
            else ca_cert_data,
            # mTLS start
            certfile=cert_file,
            keyfile=key_file,
            keypassword=key_password,
            # mTLS end
            cert_fingerprint=cert_fingerprint,
            cert_use_common_name=cert_use_common_name,
            verify_hostname=assert_hostname
            if isinstance(assert_hostname, bool)
            else True,
            assert_hostname=assert_hostname
            if isinstance(assert_hostname, str)
            else None,
            idle_timeout=self._keepalive_delay or 300.0,
        )

        self.is_verified = not self.__custom_tls_settings.insecure

        return True

    def __altsvc_probe(self, svc: str = "h3") -> tuple[str, int] | None:
        """Determine if remote yield support for an alternative service protocol."""
        # need at least first request being made
        assert self._response is not None

        for alt_svc in self._response.msg.getlist("alt-svc"):
            for protocol, alt_authority in parse_alt_svc(alt_svc):
                if protocol != svc:
                    continue

                server, port = alt_authority.split(":")

                # Security: We don't accept Alt-Svc with switching Host
                # It's up to consideration, can be a security risk.
                if server and server != self.host:
                    continue

                try:
                    return server, int(port)
                except ValueError:
                    pass

        return None

    def _post_conn(self) -> None:
        if self._tunnel_host is None:
            assert self._protocol is None, (
                "_post_conn() must be called when socket is closed or unset"
            )
        assert self.sock is not None, (
            "probable attempt to call _post_conn() prior to successful _new_conn()"
        )

        # first request was not made yet // need to infer what protocol to use.
        if self._svn is None:
            # if we are on a TLS connection, inspect ALPN.
            is_tcp_tls_conn = isinstance(self.sock, (ssl.SSLSocket, SSLTransport))

            if is_tcp_tls_conn:
                alpn: str | None = (
                    self.sock.selected_alpn_protocol()
                    if isinstance(self.sock, ssl.SSLSocket)
                    else self.sock.sslobj.selected_alpn_protocol()  # type: ignore[attr-defined]
                )

                if alpn is not None:
                    if alpn == "h2":
                        self._protocol = HTTPProtocolFactory.new(HTTP2Protocol)  # type: ignore[type-abstract]
                        self._svn = HttpVersion.h2
                    elif alpn == "http/1.1":
                        self._protocol = HTTPProtocolFactory.new(HTTP1Protocol)  # type: ignore[type-abstract]
                        self._svn = HttpVersion.h11
                    else:
                        raise ProtocolError(  # Defensive: This should be unreachable as ALPN is explicit higher in the stack.
                            f"Unsupported ALPN '{alpn}' during handshake. Did you try to reach a non-HTTP server ?"
                        )
                else:
                    # no-alpn, let's decide between H2 or H11
                    # by default, try HTTP/1.1
                    if HttpVersion.h11 not in self._disabled_svn:
                        self._protocol = HTTPProtocolFactory.new(HTTP1Protocol)  # type: ignore[type-abstract]
                        self._svn = HttpVersion.h11
                    elif HttpVersion.h2 not in self._disabled_svn:
                        self._protocol = HTTPProtocolFactory.new(HTTP2Protocol)  # type: ignore[type-abstract]
                        self._svn = HttpVersion.h2
                    else:
                        raise RuntimeError(
                            "No compatible protocol are enabled to emit request. You currently are connected using "
                            "TCP TLS and must have HTTP/1.1 or/and HTTP/2 enabled to pursue."
                        )
            else:
                # no-TLS, let's decide between H2 or H11
                # by default, try HTTP/1.1
                if HttpVersion.h11 not in self._disabled_svn:
                    self._protocol = HTTPProtocolFactory.new(HTTP1Protocol)  # type: ignore[type-abstract]
                    self._svn = HttpVersion.h11
                elif HttpVersion.h2 not in self._disabled_svn:
                    self._protocol = HTTPProtocolFactory.new(HTTP2Protocol)  # type: ignore[type-abstract]
                    self._svn = HttpVersion.h2
                else:
                    raise RuntimeError(
                        "No compatible protocol are enabled to emit request. You currently are connected using "
                        "TCP Unencrypted and must have HTTP/1.1 or/and HTTP/2 enabled to pursue."
                    )
        else:  # we or someone manually set the SVN / http version, so load the protocol regardless of what we know.
            if self._svn == HttpVersion.h2:
                self._protocol = HTTPProtocolFactory.new(HTTP2Protocol)  # type: ignore[type-abstract]
            elif self._svn == HttpVersion.h3:
                assert self.__custom_tls_settings is not None

                if self.__alt_authority is not None:
                    _, port = self.__alt_authority
                    server = self.host
                else:
                    server, port = self.host, self.port

                self._protocol = HTTPProtocolFactory.new(
                    HTTP3Protocol,  # type: ignore[type-abstract]
                    remote_address=(
                        self.__custom_tls_settings.assert_hostname
                        if self.__custom_tls_settings.assert_hostname
                        else server,
                        int(port),
                    ),
                    server_name=server,
                    tls_config=self.__custom_tls_settings,
                )

        self.conn_info = ConnectionInfo()
        self.conn_info.http_version = self._svn

        if hasattr(self, "_connect_timings") and self._connect_timings:
            self.conn_info.resolution_latency = self._connect_timings[0]
            self.conn_info.established_latency = self._connect_timings[1]

        #: Populating the ConnectionInfo using Python native capabilities
        if self._svn != HttpVersion.h3:
            cipher_tuple: tuple[str, str, int] | None = None

            if hasattr(self.sock, "sslobj"):
                self.conn_info.certificate_der = self.sock.sslobj.getpeercert(
                    binary_form=True
                )
                try:
                    self.conn_info.certificate_dict = self.sock.sslobj.getpeercert(
                        binary_form=False
                    )
                except ValueError:
                    # not supported on MacOS!
                    self.conn_info.certificate_dict = None

                self.conn_info.destination_address = None
                cipher_tuple = self.sock.sslobj.cipher()

                # Python 3.10+
                if hasattr(self.sock.sslobj, "get_verified_chain"):
                    chain = self.sock.sslobj.get_verified_chain()

                    # When cert_reqs=0 CPython returns an empty dict for the peer cert.
                    if not self.conn_info.certificate_dict and chain:
                        self.conn_info.certificate_dict = chain[0].get_info()

                    if (
                        len(chain) > 1
                        and Certificate is not None
                        and isinstance(chain[1], Certificate)
                        and hasattr(ssl, "PEM_cert_to_DER_cert")
                    ):
                        self.conn_info.issuer_certificate_der = (
                            ssl.PEM_cert_to_DER_cert(chain[1].public_bytes())
                        )
                        self.conn_info.issuer_certificate_dict = chain[1].get_info()

            elif hasattr(self.sock, "getpeercert"):
                self.conn_info.certificate_der = self.sock.getpeercert(binary_form=True)
                try:
                    self.conn_info.certificate_dict = self.sock.getpeercert(
                        binary_form=False
                    )
                except ValueError:
                    # not supported on MacOS!
                    self.conn_info.certificate_dict = None
                cipher_tuple = (
                    self.sock.cipher() if hasattr(self.sock, "cipher") else None
                )

                # Python 3.10+
                if hasattr(self.sock, "_sslobj") and hasattr(
                    self.sock._sslobj, "get_verified_chain"
                ):
                    chain = self.sock._sslobj.get_verified_chain()

                    # When cert_reqs=0 CPython returns an empty dict for the peer cert.
                    if not self.conn_info.certificate_dict and chain:
                        self.conn_info.certificate_dict = chain[0].get_info()

                    if (
                        len(chain) > 1
                        and Certificate is not None
                        and isinstance(chain[1], Certificate)
                        and hasattr(ssl, "PEM_cert_to_DER_cert")
                    ):
                        self.conn_info.issuer_certificate_der = (
                            ssl.PEM_cert_to_DER_cert(chain[1].public_bytes())
                        )
                        self.conn_info.issuer_certificate_dict = chain[1].get_info()

            if cipher_tuple:
                self.conn_info.cipher = cipher_tuple[0]
                if cipher_tuple[1] == "TLSv1.0":
                    self.conn_info.tls_version = ssl.TLSVersion.TLSv1
                elif cipher_tuple[1] == "TLSv1.1":
                    self.conn_info.tls_version = ssl.TLSVersion.TLSv1_1
                elif cipher_tuple[1] == "TLSv1.2":
                    self.conn_info.tls_version = ssl.TLSVersion.TLSv1_2
                elif cipher_tuple[1] == "TLSv1.3":
                    self.conn_info.tls_version = ssl.TLSVersion.TLSv1_3
                else:
                    self.conn_info.tls_version = None

            if self.conn_info.destination_address is None and hasattr(
                self.sock, "getpeername"
            ):
                self.conn_info.destination_address = self.sock.getpeername()[:2]

        # fallback to http/1.1 or http/2 with prior knowledge!
        if self._protocol is None or self._svn == HttpVersion.h11:
            if self._protocol is None and HttpVersion.h11 in self._disabled_svn:
                self._protocol = HTTPProtocolFactory.new(HTTP2Protocol)  # type: ignore[type-abstract]
                self._svn = HttpVersion.h2
            else:
                self._protocol = HTTPProtocolFactory.new(HTTP1Protocol)  # type: ignore[type-abstract]
                self._svn = HttpVersion.h11

            self.conn_info.http_version = self._svn

            if (
                self.conn_info.certificate_der
                and hasattr(self, "_connect_timings")
                and self._connect_timings
            ):
                self.conn_info.tls_handshake_latency = (
                    datetime.now(tz=timezone.utc) - self._connect_timings[-1]
                )

            self._connected_at = time.monotonic()
            return

        # we want to purposely mitigate the following scenario:
        #   "A server yield its support for HTTP/2 or HTTP/3 through Alt-Svc, but
        #    it cannot connect to the alt-svc, thus confusing the end-user on why it
        #    waits forever for the 2nd request."
        if self._max_tolerable_delay_for_upgrade is not None:
            self.sock.settimeout(self._max_tolerable_delay_for_upgrade)

        try:
            self.__exchange_until(
                HandshakeCompleted,
                receive_first=False,
            )
        except (
            ProtocolError,
            TimeoutError,
            SocketTimeout,
            ConnectionRefusedError,
            ConnectionResetError,
        ) as e:
            if self.__alt_authority is not None:
                # we want to remove invalid quic cache capability
                # because the alt-svc was probably bogus...
                if (
                    self._svn == HttpVersion.h3
                    and self._preemptive_quic_cache is not None
                ):
                    alt_key = (self.host, self.__origin_port or 443)
                    if alt_key in self._preemptive_quic_cache:
                        del self._preemptive_quic_cache[alt_key]

                # this avoid the close() to attempt re-use the (dead) sock
                self._protocol = None

                # we don't want to force downgrade if the user specifically said
                # to kill support for all other supported protocols!
                if (
                    HttpVersion.h11 not in self.disabled_svn
                    or HttpVersion.h2 not in self.disabled_svn
                ):
                    raise MustDowngradeError(
                        f"The server yielded its support for {self._svn} through the Alt-Svc header while unable to do so. "
                        f"To remediate that issue, either disable {self._svn} or reach out to the server admin."
                    ) from e
            raise

        self._connected_at = time.monotonic()

        if self._max_tolerable_delay_for_upgrade is not None:
            self.sock.settimeout(self.timeout)

        self._max_tolerable_delay_for_upgrade = (
            None  # upgrade went fine. discard the value!
        )

        #: Populating ConnectionInfo using QUIC TLS interfaces
        if isinstance(self._protocol, HTTPOverQUICProtocol):
            self.conn_info.certificate_der = self._protocol.getpeercert(
                binary_form=True
            )
            self.conn_info.certificate_dict = self._protocol.getpeercert(
                binary_form=False
            )
            self.conn_info.destination_address = self.sock.getpeername()[:2]
            self.conn_info.cipher = self._protocol.cipher()
            self.conn_info.tls_version = ssl.TLSVersion.TLSv1_3
            self.conn_info.issuer_certificate_dict = self._protocol.getissuercert(
                binary_form=False
            )
            self.conn_info.issuer_certificate_der = self._protocol.getissuercert(
                binary_form=True
            )

        if (
            self.conn_info.certificate_der
            and hasattr(self, "_connect_timings")
            and not self.conn_info.tls_handshake_latency
        ):
            self.conn_info.tls_handshake_latency = (
                datetime.now(tz=timezone.utc) - self._connect_timings[-1]
            )

    def set_tunnel(
        self,
        host: str,
        port: int | None = None,
        headers: typing.Mapping[str, str] | None = None,
        scheme: str = "http",
    ) -> None:
        if self.sock:
            # overly protective, checks are made higher, this is unreachable.
            raise RuntimeError(  # Defensive: highly controlled, should be unreachable.
                "Can't set up tunnel for established connection"
            )

        # We either support tunneling or http/3. Need complex developments.
        if HttpVersion.h3 not in self._disabled_svn:
            self._disabled_svn.add(HttpVersion.h3)

        self._tunnel_host: str | None = host
        self._tunnel_port: int | None = port

        if headers:
            self._tunnel_headers = headers
        else:
            self._tunnel_headers = {}

    def _tunnel(self) -> None:
        assert self._protocol is not None
        assert self.sock is not None
        assert self._tunnel_host is not None
        assert self._tunnel_port is not None

        if self._svn != HttpVersion.h11:
            raise NotImplementedError(
                """Unable to establish a tunnel using other than HTTP/1.1."""
            )

        self._stream_id = self._protocol.get_available_stream_id()

        req_context = [
            (
                b":authority",
                f"{self._tunnel_host}:{self._tunnel_port}".encode("ascii"),
            ),
            (b":method", b"CONNECT"),
        ]

        for header, value in self._tunnel_headers.items():
            req_context.append((header.lower().encode(), value.encode("iso-8859-1")))

        self._protocol.submit_headers(
            self._stream_id,
            req_context,
            True,
        )

        events = self.__exchange_until(
            HeadersReceived,
            receive_first=False,
            event_type_collectable=(HeadersReceived,),
            # special case for CONNECT
            respect_end_stream_signal=False,
        )

        status: int | None = None

        for event in events:
            if isinstance(event, HeadersReceived):
                for raw_header, raw_value in event.headers:
                    if raw_header == b":status":
                        status = int(raw_value.decode())
                        break

        tunnel_accepted: bool = status is not None and (200 <= status < 300)

        if not tunnel_accepted:
            self.close()
            message: str = (
                responses[status] if status and status in responses else "Unknown"
            )
            raise OSError(f"Tunnel connection failed: {status} {message}")

        # We will re-initialize those afterward
        # to be in phase with Us --> NotIntermediary
        self._svn = None
        self._protocol = None
        self._protocol_factory = None

    def peek_and_react(self, expect_frame: bool = False) -> bool:
        """This method should be called by a thread using TrafficPolice when it is idle.
        Multiplexed protocols can receive incoming data unsolicited. Like when using QUIC
        or when reaching a WebSocket.
        This method return True if there is any event ready to unpack for the connection.
        Some server implementation may be aggressive toward "idle" session
        this is especially true when using QUIC.
        For example, google/quiche send regular unsolicited data and expect regular ACKs, otherwise will
        deduct that network conn is dead.
        see: https://github.com/google/quiche/commit/c4bb0723f0a03e135bc9328b59a39382761f3de6
        and: https://github.com/google/quiche/blob/92b45f743288ea2f43ae8cdc4a783ef252e41d93/quiche/quic/core/quic_connection.cc#L6322
        """
        if self.sock is None or self._protocol is None:
            return False

        bck_timeout = self.sock.gettimeout()

        self.sock.settimeout(0.001 if not expect_frame else 0.1)

        try:
            peek_data = self.sock.recv(self.blocksize)
        except (OSError, TimeoutError, socket.timeout):
            return False
        except (ConnectionAbortedError, ConnectionResetError):
            peek_data = b""
        finally:
            self.sock.settimeout(bck_timeout)

        if not peek_data:
            # connection loss...
            self._protocol.connection_lost()
            return False

        try:
            self._protocol.bytes_received(peek_data)
        except self._protocol.exceptions():
            return False

        while True:
            data_out = self._protocol.bytes_to_send()

            if not data_out:
                break

            try:
                self.sock.sendall(data_out)
            except OSError:
                return False

        self._last_used_at = time.monotonic()

        return self._protocol.has_pending_event()

    def __exchange_until(
        self,
        event_type: type[Event] | tuple[type[Event], ...],
        *,
        receive_first: bool = False,
        event_type_collectable: type[Event] | tuple[type[Event], ...] | None = None,
        respect_end_stream_signal: bool = True,
        maximal_data_in_read: int | None = None,
        data_in_len_from: typing.Callable[[Event], int] | None = None,
        stream_id: int | None = None,
    ) -> list[Event]:
        """This method simplify socket exchange in/out based on what the protocol state machine orders.
        Can be used for the initial handshake for instance."""
        assert self.sock is not None and self._protocol is not None

        if maximal_data_in_read is not None:
            if not (maximal_data_in_read >= 0 or maximal_data_in_read == -1):
                maximal_data_in_read = None

        data_out: bytes
        data_in: bytes

        data_in_len: int = 0

        events: list[Event] = []
        reshelve_events: list[Event] = []

        if maximal_data_in_read == 0:
            # The '0' case amt is handled higher in the stack.
            return events  # Defensive: This should be unreachable in the current project state.

        if maximal_data_in_read and maximal_data_in_read < 0:
            respect_end_stream_signal = False
            maximal_data_in_read = None
            data_in_len_from = None

        while True:
            reach_socket: bool = False
            if not self._protocol.has_pending_event(stream_id=stream_id):
                if receive_first is False:
                    while True:
                        data_out = self._protocol.bytes_to_send()

                        if not data_out:
                            break

                        self.sock.sendall(data_out)

                try:
                    data_in = self.sock.recv(self.blocksize)
                except (ConnectionAbortedError, ConnectionResetError) as e:
                    if isinstance(e, ConnectionResetError) and (
                        event_type is HandshakeCompleted
                        or (
                            isinstance(event_type, tuple)
                            and HandshakeCompleted in event_type
                        )
                    ):
                        raise e
                    data_in = b""

                reach_socket = True

                if not data_in:
                    # in some cases (merely http/1 legacy)
                    # server can signify "end-of-transmission" by simply closing the socket.
                    # pretty much dirty.

                    # must have at least one event received, otherwise we can't declare a proper eof.
                    if (events or self._response is not None) and hasattr(
                        self._protocol, "eof_received"
                    ):
                        try:
                            self._protocol.eof_received()
                        except self._protocol.exceptions() as e:  # Defensive:
                            # overly protective, we hide exception that are behind urllib3.
                            # should not happen, but one truly never known.
                            raise ProtocolError(e) from e  # Defensive:
                    else:
                        self._protocol.connection_lost()
                else:
                    if data_in_len_from is None:
                        data_in_len += len(data_in)

                    try:
                        self._protocol.bytes_received(data_in)
                    except self._protocol.exceptions() as e:
                        # h2 has a dedicated exception for IncompleteRead (InvalidBodyLengthError)
                        # we convert the exception to our "IncompleteRead" instead.
                        if hasattr(e, "expected_length") and hasattr(
                            e, "actual_length"
                        ):
                            raise IncompleteRead(
                                partial=e.actual_length, expected=e.expected_length
                            ) from e  # Defensive:
                        raise ProtocolError(e) from e  # Defensive:

                if receive_first is True:
                    while True:
                        data_out = self._protocol.bytes_to_send()

                        if not data_out:
                            break

                        self.sock.sendall(data_out)

            for event in self._protocol.events(stream_id=stream_id):  # type: Event
                stream_related_event: bool = hasattr(event, "stream_id")

                if not stream_related_event and isinstance(event, ConnectionTerminated):
                    if event.error_code == 0 and self._response is not None:
                        self._protocol = None
                        self.close()
                        raise MustRedialError(
                            f"Remote peer just closed our connection, probably for not answering to unsolicited packet. ({event.message})"
                        )

                    # we can receive a zero-length payload, that usually means the remote closed the socket.
                    # but we should be able to redial. we only attempt this when we've just sent a request.
                    # this cond. requires to have one successful response out of this conn, that demonstrate
                    # the connection is usable. (avoid loop in retries)
                    if (
                        reach_socket is True
                        and data_in == b""
                        and self._response is not None
                        and (
                            (
                                isinstance(event_type, tuple)
                                and HeadersReceived in event_type
                            )
                            or (event_type is HeadersReceived)
                        )
                        and all(isinstance(e, HeadersReceived) is False for e in events)
                    ):
                        self._protocol = None
                        self.close()
                        raise MustRedialError(
                            "Server unexpectedly closed the connection in-flight (connection dropped)"
                        )

                    if (
                        event.error_code == 400
                        and event.message
                        and "header" in event.message
                    ):
                        raise InvalidHeader(event.message)
                    # QUIC operate TLS verification outside native capabilities
                    # We have to forward the error so that users aren't caught off guard when the connection
                    # unexpectedly close.
                    elif event.error_code == 298 and self._svn == HttpVersion.h3:
                        if event.message and "Fingerprint" in event.message:
                            raise SSLError(
                                f"TLS over QUIC did not succeed. {event.message}"
                            )
                        else:
                            raise SSLError(
                                "TLS over QUIC did not succeed. Chain certificate verification failed "
                                "or client cert validation failed."
                            )

                    # we shall convert the ProtocolError to IncompleteRead
                    # so that users aren't caught off guard.
                    try:
                        if (
                            event.message
                            and "without sending complete message body" in event.message
                        ):
                            msg = event.message.replace(
                                "peer closed connection without sending complete message body ",
                                "",
                            ).strip("()")

                            received, expected = (
                                int("".join(c for c in _ if c.isdigit()).strip())
                                for _ in tuple(msg.split(", "))
                            )

                            raise IncompleteRead(
                                partial=received,
                                expected=expected - received,
                            )
                    except (ValueError, IndexError):
                        pass

                    raise ProtocolError(event.message)
                elif stream_related_event and isinstance(event, StreamResetReceived):
                    # we want to catch MUST_USE_HTTP_1_1 or H3_VERSION_FALLBACK
                    # HTTP/2 https://www.rfc-editor.org/rfc/rfc9113.html#name-error-codes
                    # HTTP/3 https://www.iana.org/assignments/http3-parameters/http3-parameters.xhtml#http3-parameters-error-codes
                    if (self._svn == HttpVersion.h2 and event.error_code == 0xD) or (
                        self._svn == HttpVersion.h3 and event.error_code == 0x0110
                    ):
                        if HttpVersion.h11 not in self.disabled_svn:
                            raise MustDowngradeError(
                                f"The remote server is unable to serve this resource over {self._svn}"
                            )
                    raise ProtocolError(
                        f"Stream {event.stream_id} was reset by remote peer. Reason: {hex(event.error_code)}."
                    )

                if not event_type_collectable:
                    events.append(event)
                else:
                    if isinstance(event, event_type_collectable):
                        events.append(event)

                        if data_in_len_from is not None:
                            try:
                                data_in_len += data_in_len_from(event)
                            except AttributeError:
                                pass
                    else:
                        reshelve_events.append(event)

                target_cap_reached: bool = (
                    maximal_data_in_read is not None
                    and data_in_len >= maximal_data_in_read
                )

                if (event_type and isinstance(event, event_type)) or target_cap_reached:
                    # if event type match, make sure it is the latest one
                    # simply put, end_stream should be True.
                    if (
                        target_cap_reached is False
                        and respect_end_stream_signal
                        and stream_related_event
                    ):
                        if event.end_stream is True:  # type: ignore[attr-defined]
                            if reshelve_events:
                                self._protocol.reshelve(*reshelve_events)
                            return events
                        continue

                    if reshelve_events:
                        self._protocol.reshelve(*reshelve_events)
                    return events
                elif (
                    stream_related_event
                    and event.end_stream is True  # type: ignore[attr-defined]
                    and respect_end_stream_signal is True
                ):
                    if reshelve_events:
                        self._protocol.reshelve(*reshelve_events)
                    return events
                elif (
                    stream_related_event
                    and event.end_stream is True  # type: ignore[attr-defined]
                    and maximal_data_in_read is None
                    and event_type_collectable is not None
                    and stream_id == event.stream_id  # type: ignore[attr-defined]
                ):
                    if reshelve_events:
                        self._protocol.reshelve(*reshelve_events)
                    return events

    def putrequest(
        self,
        method: str,
        url: str,
        skip_host: bool = False,
        skip_accept_encoding: bool = False,
    ) -> None:
        """Internally fhace translate this into what putrequest does. e.g. initial trame."""
        self.__headers = []
        self.__expected_body_length = None
        self.__remaining_body_length = None
        self.__legacy_host_entry = None
        self.__authority_bit_set = False
        self.__protocol_bit_set = False

        self._start_last_request = datetime.now(tz=timezone.utc)

        if self._tunnel_host is not None:
            host, port = self._tunnel_host, self._tunnel_port
        else:
            host, port = self.host, self.port

        self.__headers = [
            (b":method", method.encode("ascii")),
            (
                b":scheme",
                self.scheme.encode("ascii"),
            ),
            (b":path", url.encode("ascii")),
        ]

        if not skip_host:
            authority: bytes = host.encode("idna")
            self.__headers.append(
                (
                    b":authority",
                    authority
                    if port == self.default_port or port is None  # type: ignore[attr-defined]
                    else authority + f":{port}".encode(),
                ),
            )
            self.__authority_bit_set = True

        if not skip_accept_encoding:
            self.__headers.append(
                (
                    b"accept-encoding",
                    b"identity",
                )
            )

    def putheader(self, header: str, *values: str) -> None:
        # note: requests allow passing headers as bytes (seen in requests/tests)
        # warn: always lowercase header names, quic transport crash if not lowercase.
        header = header.lower()

        encoded_header = header.encode("ascii") if isinstance(header, str) else header

        # only h11 support chunked transfer encoding, we internally translate
        # it to the right method for h2 and h3.
        support_te_chunked: bool = self._svn == HttpVersion.h11

        # We MUST never use that header in h2 and h3 over quic.
        # Passing 'Connection' header is actually a protocol violation above h11.
        # We assume it is passed as-is (meaning 'keep-alive' lower-cased)
        # It may(should) break the connection.
        if not support_te_chunked:
            if encoded_header in {
                b"transfer-encoding",
                b"connection",
                b"upgrade",
                b"keep-alive",
            }:
                return

        if self.__expected_body_length is None and encoded_header == b"content-length":
            try:
                self.__expected_body_length = int(values[0])
            except ValueError:
                raise ProtocolError(
                    f"Invalid content-length set. Given '{values[0]}' when only digits are allowed."
                )
        elif self.__legacy_host_entry is None and encoded_header == b"host":
            self.__legacy_host_entry = (
                values[0].encode("idna") if isinstance(values[0], str) else values[0]
            )
            return

        for value in values:
            encoded_value = (
                value.encode("iso-8859-1") if isinstance(value, str) else value
            )

            if encoded_header.startswith(b":"):
                if encoded_header == b":protocol":
                    self.__protocol_bit_set = True
                item_to_remove = None

                for _k, _v in self.__headers:
                    if not _k.startswith(b":"):
                        break
                    if _k == encoded_header:
                        item_to_remove = (_k, _v)
                        break

                if item_to_remove is not None:
                    self.__headers.remove(item_to_remove)

                self.__headers.insert(
                    0,
                    (
                        encoded_header,
                        encoded_value,
                    ),
                )
            else:
                self.__headers.append(
                    (
                        encoded_header,
                        encoded_value,
                    )
                )

    def endheaders(
        self,
        message_body: bytes | None = None,
        *,
        encode_chunked: bool = False,
        expect_body_afterward: bool = False,
    ) -> ResponsePromise | None:
        if self.sock is None:
            self.connect()  # type: ignore[attr-defined]

        # some libraries override connect(), thus bypassing our state machine initialization.
        if self._protocol is None:
            self._post_conn()

        assert self.sock is not None and self._protocol is not None

        # only h2 and h3 support streams, it is faked/simulated for h1.
        self._stream_id = self._protocol.get_available_stream_id()
        # unless anything hint the opposite, the request head frame is the end stream
        should_end_stream: bool = (
            expect_body_afterward is False and self.__protocol_bit_set is False
        )

        # handle cases where 'Host' header is set manually
        if self.__legacy_host_entry is not None:
            existing_authority = None

            for cursor_header, cursor_value in self.__headers:
                if cursor_header == b":authority":
                    existing_authority = (cursor_header, cursor_value)
                    break
                if not cursor_header.startswith(b":"):
                    break

            if existing_authority is not None:
                self.__headers.remove(existing_authority)

            self.__headers.insert(3, (b":authority", self.__legacy_host_entry))
            self.__authority_bit_set = True

        if self.__authority_bit_set is False:
            raise ProtocolError(
                (
                    "urllib3.future do not support emitting HTTP requests without the `Host` header ",
                    "It was only permitted in HTTP/1.0 and prior. This client support HTTP/1.1+.",
                )
            )

        try:
            self._protocol.submit_headers(
                self._stream_id,
                self.__headers,
                end_stream=should_end_stream,
            )
        except self._protocol.exceptions() as e:  # Defensive:
            # overly protective, designed to avoid exception leak bellow urllib3.
            raise ProtocolError(e) from e  # Defensive:

        try:
            while True:
                buf = self._protocol.bytes_to_send()
                if not buf:
                    break
                self.sock.sendall(buf)
        except BrokenPipeError as e:
            rp = ResponsePromise(self, self._stream_id, self.__headers)
            self._promises[rp.uid] = rp
            self._promises_per_stream[rp.stream_id] = rp
            e.promise = rp  # type: ignore[attr-defined]

            raise e
        else:
            self._last_used_at = time.monotonic()

        if expect_body_afterward is False:
            if self._start_last_request and self.conn_info:
                self.conn_info.request_sent_latency = (
                    datetime.now(tz=timezone.utc) - self._start_last_request
                )

            rp = ResponsePromise(self, self._stream_id, self.__headers)
            self._promises[rp.uid] = rp
            self._promises_per_stream[rp.stream_id] = rp
            return rp

        return None

    def __write_st(
        self, __buf: bytes, __stream_id: int, __close_stream: bool = False
    ) -> None:
        assert self._protocol is not None
        assert self.sock is not None

        while (
            self._protocol.should_wait_remote_flow_control(__stream_id, len(__buf))
            is True
        ):
            self._protocol.bytes_received(self.sock.recv(self.blocksize))

            while True:
                data_out = self._protocol.bytes_to_send()

                if not data_out:
                    break

                self.sock.sendall(data_out)

        self._protocol.submit_data(
            __stream_id,
            __buf,
            end_stream=__close_stream,
        )

        while True:
            data_out = self._protocol.bytes_to_send()

            if not data_out:
                break

            self.sock.sendall(data_out)

        self._last_used_at = time.monotonic()

    def __abort_st(self, __stream_id: int) -> None:
        """Kill a stream properly."""
        assert self._protocol is not None
        assert self.sock is not None

        self._protocol.submit_stream_reset(stream_id=__stream_id)

        while True:
            data_out = self._protocol.bytes_to_send()

            if not data_out:
                break

            self.sock.sendall(data_out)

        try:
            del self._pending_responses[__stream_id]
        except KeyError:
            pass  # Hmm... this should be impossible.

        # remote can refuse future inquiries, so no need to go further with this conn.
        if self._protocol.has_expired():
            self.close()

        self._last_used_at = time.monotonic()

    def __read_st(
        self,
        __amt: int | None,
        __stream_id: int | None,
        __respect_end_signal: bool = True,
        __dummy_operation: bool = False,
    ) -> tuple[bytes, bool, HTTPHeaderDict | None]:
        """Allows us to defer the body loading after constructing the response object."""

        # we may want to just remove the response as "pending"
        # e.g. HTTP Extension; making reads on sub protocol close may
        # ends up in a blocking situation (forever).
        if __dummy_operation:
            try:
                del self._pending_responses[__stream_id]  # type: ignore[arg-type]
            except KeyError:
                pass  # Hmm... this should be impossible.

            # remote can refuse future inquiries, so no need to go further with this conn.
            if self._protocol.is_idle() and self._protocol.has_expired():  # type: ignore[union-attr]
                self.close()
            return b"", True, None

        eot = False

        events: list[DataReceived | HeadersReceived] = self.__exchange_until(  # type: ignore[assignment]
            DataReceived,
            receive_first=True,
            event_type_collectable=(DataReceived, HeadersReceived),
            maximal_data_in_read=__amt,
            data_in_len_from=lambda x: len(x.data),  # type: ignore[attr-defined]
            stream_id=__stream_id,
            respect_end_stream_signal=__respect_end_signal,
        )

        self._last_used_at = time.monotonic()

        if events and events[-1].end_stream:
            eot = True

            try:
                del self._pending_responses[__stream_id]  # type: ignore[arg-type]
            except KeyError:
                pass  # Hmm... this should be impossible.

            # remote can refuse future inquiries, so no need to go further with this conn.
            if self._protocol.is_idle() and self._protocol.has_expired():  # type: ignore[union-attr]
                self.close()
            elif self.is_idle:
                # probe for h3/quic if available, and remember it.
                self._upgrade()

        trailers = None

        if eot:
            idx = None

            if isinstance(events[-1], HeadersReceived):
                idx = -1
            elif len(events) >= 2 and isinstance(events[-2], HeadersReceived):
                idx = -2

            # http-trailers SHOULD be received LAST!
            # but we should tolerate a DataReceived of len=0 last, just in case.
            if idx is not None:
                trailers = HTTPHeaderDict()

                for raw_header, raw_value in events[idx].headers:  # type: ignore[union-attr]
                    # ignore...them? special headers. aka. starting with semicolon
                    if raw_header[0] == 0x3A:
                        continue
                    else:
                        trailers.add(
                            raw_header.decode("ascii"), raw_value.decode("iso-8859-1")
                        )

                events.pop(idx)

                if not events:
                    return b"", True, trailers

        return (
            b"".join(e.data for e in events) if len(events) > 1 else events[0].data,  # type: ignore[union-attr]
            eot,
            trailers,
        )

    def getresponse(
        self,
        *,
        promise: ResponsePromise | None = None,
        early_response_callback: typing.Callable[[LowLevelResponse], None]
        | None = None,
    ) -> LowLevelResponse:
        if (
            self.sock is None  # Didn't we establish a connection?
            or self._protocol is None  # Did we initialize the state-machine protocol?
            or not self._promises  # Do we have at least one ResponsePromise pending?
        ):
            raise ResponseNotReady()  # Defensive: Comply with http.client behavior.

        # Usually, will be a single event in array. We should be able to handle the case >1 too, but we actually don't.
        head_event: HeadersReceived | EarlyHeadersReceived = self.__exchange_until(  # type: ignore[assignment]
            (
                HeadersReceived,
                EarlyHeadersReceived,
            ),
            receive_first=True,
            event_type_collectable=(
                HeadersReceived,
                EarlyHeadersReceived,
            ),
            respect_end_stream_signal=False,  # Stop as soon as we get either (collectable) event.
            stream_id=promise.stream_id if promise else None,
        ).pop()

        # we want to have a view on last conn was used
        # ...in the sense that we spoke with the remote peer.
        self._last_used_at = time.monotonic()

        headers = HTTPHeaderDict()
        status: int | None = None

        for raw_header, raw_value in head_event.headers:
            # special headers that represent (usually) the HTTP response status, version and reason.
            if status is None and raw_header[0] == 0x3A:
                if raw_header == b":status":
                    status = int(raw_value)
            else:
                headers.add(raw_header.decode("ascii"), raw_value.decode("iso-8859-1"))

        # this should be unreachable
        if status is None:
            raise ProtocolError(  # Defensive: This is unreachable, all three implementations crash before.
                "Got an HTTP response without a status code. This is a violation."
            )

        # 101 = Switching Protocol! It's our final HTTP response, but the stream remains open!
        is_early_response = (
            isinstance(head_event, EarlyHeadersReceived) and status != 101
        )

        if promise is None:
            try:
                if is_early_response:
                    promise = self._promises_per_stream[head_event.stream_id]
                else:
                    promise = self._promises_per_stream.pop(head_event.stream_id)
            except KeyError as e:
                raise ProtocolError(
                    f"Response received (stream: {head_event.stream_id}) but no promise in-flight"
                ) from e
        else:
            if not is_early_response:
                del self._promises_per_stream[promise.stream_id]

        http_verb = b""

        for raw_header, raw_value in promise.request_headers:
            if raw_header == b":method":
                http_verb = raw_value
                break

        try:
            reason: str = responses[status]
        except KeyError:
            reason = "Unknown"

        if is_early_response:
            if early_response_callback is not None:
                early_response = LowLevelResponse(
                    http_verb.decode("ascii"),
                    status,
                    self._http_vsn,
                    reason,
                    headers,
                    body=None,
                    authority=self.host,
                    port=self.port,
                    stream_id=promise.stream_id,
                )
                early_response.from_promise = promise

                early_response_callback(early_response)

            return HfaceBackend.getresponse(
                self, promise=promise, early_response_callback=early_response_callback
            )

        eot = head_event.end_stream is True

        if status == 101 or (http_verb == b"CONNECT" and (200 <= status < 300)):
            dsa: DirectStreamAccess | None = DirectStreamAccess(
                promise.stream_id,
                self.__read_st,
                self.__write_st,
            )
        else:
            dsa = None

        self._response: LowLevelResponse = LowLevelResponse(
            http_verb.decode("ascii"),
            status,
            self._http_vsn,
            reason,
            headers,
            self.__read_st if eot is False and dsa is None else None,
            authority=self.host,
            port=self.port,
            stream_id=promise.stream_id,
            sock=self.sock
            if self._http_vsn == 11
            else None,  # kept for BC purposes[...] one should not try to read from it.
            dsa=dsa,
            stream_abort=self.__abort_st if eot is False else None,
        )
        promise.response = self._response
        self._response.from_promise = promise

        # we delivered a response, we can safely remove the promise from queue.
        del self._promises[promise.uid]

        if eot:
            # remote can refuse future inquiries, so no need to go further with this conn.
            if self._protocol.has_expired():
                self.close()
            elif self.is_idle:
                self._upgrade()
        else:
            self._pending_responses[promise.stream_id] = self._response

        return self._response

    def send(
        self,
        data: (bytes | typing.IO[typing.Any] | typing.Iterable[bytes] | str),
        *,
        eot: bool = False,
    ) -> ResponsePromise | None:
        """We might be receiving a chunk constructed downstream"""
        if self.sock is None or self._stream_id is None or self._protocol is None:
            # this is unreachable in normal condition as urllib3
            # is strict on his workflow.
            raise RuntimeError(  # Defensive:
                "Trying to send data from a closed connection"
            )

        if (
            self.__remaining_body_length is None
            and self.__expected_body_length is not None
        ):
            self.__remaining_body_length = self.__expected_body_length

        try:
            if isinstance(
                data,
                (
                    bytes,
                    bytearray,
                ),
            ):
                while (
                    self._protocol.should_wait_remote_flow_control(
                        self._stream_id, len(data)
                    )
                    is True
                ):
                    self._protocol.bytes_received(self.sock.recv(self.blocksize))

                    # this is a bad sign. we should stop sending and instead retrieve the response.
                    # we exclude 'EarlyHeadersReceived' event because it is simply expected. e.g. 100-continue!
                    if self._protocol.has_pending_event(
                        stream_id=self._stream_id, excl_event=(EarlyHeadersReceived,)
                    ):
                        if self._start_last_request and self.conn_info:
                            self.conn_info.request_sent_latency = (
                                datetime.now(tz=timezone.utc) - self._start_last_request
                            )

                        rp = ResponsePromise(self, self._stream_id, self.__headers)
                        self._promises[rp.uid] = rp
                        self._promises_per_stream[rp.stream_id] = rp

                        raise EarlyResponse(promise=rp)

                    while True:
                        data_out = self._protocol.bytes_to_send()

                        if not data_out:
                            break

                        self.sock.sendall(data_out)

                if self.__remaining_body_length:
                    self.__remaining_body_length -= len(data)

                self._protocol.submit_data(
                    self._stream_id,
                    data,
                    end_stream=eot,
                )
            else:
                # urllib3 is supposed to handle every case
                # and pass down bytes only. This should be unreachable.
                raise OSError(  # Defensive:
                    f"unhandled type '{type(data)}' in send method"
                )

            if _HAS_SYS_AUDIT:
                sys.audit("http.client.send", self, data)

            remote_pipe_shutdown: BrokenPipeError | None = None

            # some protocols may impose regulated frame size
            # so expect multiple frame per send()
            while True:
                data_out = self._protocol.bytes_to_send()

                if not data_out:
                    break

                try:
                    self.sock.sendall(data_out)
                except BrokenPipeError as e:
                    remote_pipe_shutdown = e

            if eot or remote_pipe_shutdown:
                if self._start_last_request and self.conn_info:
                    self.conn_info.request_sent_latency = (
                        datetime.now(tz=timezone.utc) - self._start_last_request
                    )

                rp = ResponsePromise(self, self._stream_id, self.__headers)
                self._promises[rp.uid] = rp
                self._promises_per_stream[rp.stream_id] = rp
                if remote_pipe_shutdown:
                    remote_pipe_shutdown.promise = rp  # type: ignore[attr-defined]
                    raise remote_pipe_shutdown
                return rp

        except self._protocol.exceptions() as e:
            raise ProtocolError(  # Defensive: In the unlikely event that exception may leak from below
                e
            ) from e

        return None

    def ping(self) -> None:
        """Send a ping frame if possible. Otherwise, fail silently."""
        if self.sock is None or self._protocol is None:
            return

        if self._protocol.has_expired():
            return

        try:
            self._protocol.ping()
        except NotImplementedError:
            return
        except self._protocol.exceptions():
            return

        while True:
            ping_frame = self._protocol.bytes_to_send()

            if not ping_frame:
                break

            try:
                self.sock.sendall(ping_frame)
            except (
                OSError,
                ssl.SSLError,
            ):
                break

        self._last_used_at = time.monotonic()

    def close(self) -> None:
        if self.sock:
            if self._protocol is not None:
                try:
                    self._protocol.submit_close()
                except self._protocol.exceptions() as e:  # Defensive:
                    # overly protective, made in case of possible exception leak.
                    raise ProtocolError(e) from e  # Defensive:
                else:
                    while True:
                        goodbye_frame = self._protocol.bytes_to_send()
                        if not goodbye_frame:
                            break
                        try:
                            self.sock.sendall(goodbye_frame)
                        except (
                            OSError,
                            ssl.SSLEOFError,
                        ):  # don't want our goodbye, never mind then!
                            break

            try:
                self.sock.shutdown(0)
            except (OSError, AttributeError):
                pass

            try:
                self.sock.close()
            except OSError:
                pass

        self._protocol = None
        self._stream_id = None
        self._promises = {}
        self._promises_per_stream = {}
        self._pending_responses = {}
        self.__custom_tls_settings = None
        self.conn_info = None
        self.__expected_body_length = None
        self.__remaining_body_length = None
        self._start_last_request = None
        self._cached_http_vsn = None
        self._connected_at = None
        self._last_used_at = time.monotonic()
