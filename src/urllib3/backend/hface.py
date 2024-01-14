from __future__ import annotations

import socket
import sys
import typing
from datetime import datetime, timezone
from socket import SOCK_DGRAM, SOCK_STREAM

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
from .._constant import DEFAULT_BLOCKSIZE, responses
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
    Event,
    HandshakeCompleted,
    HeadersReceived,
    StreamResetReceived,
)
from ..exceptions import (
    EarlyResponse,
    IncompleteRead,
    InvalidHeader,
    ProtocolError,
    ResponseNotReady,
    SSLError,
)
from ..util import parse_alt_svc, resolve_cert_reqs
from ._base import (
    BaseBackend,
    ConnectionInfo,
    HttpVersion,
    LowLevelResponse,
    QuicPreemptiveCacheType,
    ResponsePromise,
)

if typing.TYPE_CHECKING:
    from .._typing import _TYPE_SOCKET_OPTIONS

_HAS_SYS_AUDIT = hasattr(sys, "audit")
_HAS_HTTP3_SUPPORT = HTTPProtocolFactory.has(HTTP3Protocol)  # type: ignore[type-abstract]


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
    ):
        if not _HAS_HTTP3_SUPPORT:
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

        # h3 specifics
        self.__custom_tls_settings: QuicTLSConfig | None = None
        self.__alt_authority: tuple[str, int] | None = None
        self.__session_ticket: typing.Any | None = None

    @property
    def is_saturated(self) -> bool:
        if self._protocol is None:
            return True
        return self._protocol.is_available() is False

    @property
    def is_multiplexed(self) -> bool:
        return self._protocol is not None and self._protocol.multiplexed

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

        if self._svn == HttpVersion.h3:
            self.socket_kind = SOCK_DGRAM

            # undo local memory on whether conn supposedly support quic/h3
            # if conn target another host.
            if self._response and self._response.authority != self.host:
                self._svn = None
                self._new_conn()  # restore socket defaults
        else:
            self.socket_kind = SOCK_STREAM

        return None

    def _upgrade(self) -> None:
        assert (
            self._response is not None
        ), "attempt to call _upgrade() prior to successful getresponse()"
        assert self.sock is not None
        assert self._svn is not None

        if not _HAS_HTTP3_SUPPORT:
            return

        # do not upgrade if not coming from TLS already.
        # we exclude SSLTransport, HTTP/3 is not supported in that condition anyway.
        if type(self.sock) == socket.socket:
            return

        if self._svn == HttpVersion.h3:
            return
        if HttpVersion.h3 in self._disabled_svn:
            return

        self.__alt_authority = self.__h3_probe()

        if self.__alt_authority:
            if self._preemptive_quic_cache is not None:
                self._preemptive_quic_cache[
                    (self.host, self.port or 443)
                ] = self.__alt_authority
            self._svn = HttpVersion.h3
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
    ) -> None:
        """Meant to support TLS over QUIC meanwhile cpython does not ship with its native implementation."""
        if self._svn != HttpVersion.h3:
            raise NotImplementedError

        cert_use_common_name = False

        allow_insecure: bool = False

        if ssl_context:
            cert_use_common_name = (
                getattr(ssl_context, "hostname_checks_common_name", False) or False
            )

            if ssl_context.verify_mode == ssl.CERT_NONE:
                allow_insecure = True

        if not allow_insecure and resolve_cert_reqs(cert_reqs) == ssl.CERT_NONE:
            allow_insecure = True

        self.__custom_tls_settings = QuicTLSConfig(
            insecure=allow_insecure,
            cafile=ca_certs,
            capath=ca_cert_dir,
            cadata=ca_cert_data.encode()
            if isinstance(ca_cert_data, str)
            else ca_cert_data,
            session_ticket=self.__session_ticket,  # going to be set after first successful quic handshake
            # mTLS start
            certfile=cert_file,
            keyfile=key_file,
            keypassword=key_password,
            # mTLS end
            cert_fingerprint=cert_fingerprint,
            cert_use_common_name=cert_use_common_name,
            verify_hostname=bool(assert_hostname),
            assert_hostname=assert_hostname
            if isinstance(assert_hostname, str)
            else None,
        )

        self.is_verified = not self.__custom_tls_settings.insecure

    def __h3_probe(self) -> tuple[str, int] | None:
        """Determine if remote is capable of operating through the http/3 protocol over QUIC."""
        # need at least first request being made
        assert self._response is not None

        for alt_svc in self._response.msg.getlist("alt-svc"):
            for protocol, alt_authority in parse_alt_svc(alt_svc):
                # Looking for final specification of HTTP/3 over QUIC.
                if protocol != "h3":
                    continue

                server, port = alt_authority.split(":")

                # Security: We don't accept Alt-Svc with switching Host
                # It's up to consideration, can be a security risk.
                if server and server != self.host:
                    continue

                return server, int(port)

        return None

    def _post_conn(self) -> None:
        if self._tunnel_host is None:
            assert (
                self._protocol is None
            ), "_post_conn() must be called when socket is closed or unset"
        assert (
            self.sock is not None
        ), "probable attempt to call _post_conn() prior to successful _new_conn()"

        # first request was not made yet
        if self._svn is None:
            if isinstance(self.sock, (ssl.SSLSocket, SSLTransport)):
                alpn: str | None = (
                    self.sock.selected_alpn_protocol()
                    if isinstance(self.sock, ssl.SSLSocket)
                    else self.sock.sslobj.selected_alpn_protocol()  # type: ignore[attr-defined]
                )

                if alpn is not None:
                    if alpn == "h2":
                        self._protocol = HTTPProtocolFactory.new(HTTP2Protocol)  # type: ignore[type-abstract]
                        self._svn = HttpVersion.h2
                    elif alpn != "http/1.1":
                        raise ProtocolError(  # Defensive: This should be unreachable as ALPN is explicit higher in the stack.
                            f"Unsupported ALPN '{alpn}' during handshake"
                        )
        else:
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
                if cipher_tuple[1] == "TLSv1.1":
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

        # fallback to http/1.1
        if self._protocol is None or self._svn == HttpVersion.h11:
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

            return

        # it may be required to send some initial data, aka. magic header (PRI * HTTP/2..)
        self.__exchange_until(
            HandshakeCompleted,
            receive_first=False,
        )

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

        if hasattr(self, "_connect_timings"):
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
        assert self._protocol is not None
        assert self.sock is not None
        assert maximal_data_in_read is None or (
            maximal_data_in_read >= 0 or maximal_data_in_read == -1
        )

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
            if not self._protocol.has_pending_event():
                if receive_first is False:
                    while True:
                        data_out = self._protocol.bytes_to_send()

                        if not data_out:
                            break

                        self.sock.sendall(data_out)

                data_in = self.sock.recv(self.blocksize)

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
                        raise ProtocolError(
                            "server unexpectedly closed the connection in-flight (prior-to-response)"
                        )
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

            for event in iter(self._protocol.next_event, None):  # type: Event
                if stream_id is not None and hasattr(event, "stream_id"):
                    if event.stream_id != stream_id:
                        reshelve_events.append(event)
                        continue

                if isinstance(event, ConnectionTerminated):
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
                        raise SSLError(
                            "TLS over QUIC did not succeed (Error 298). Chain certificate verification failed."
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

                            received, expected = tuple(msg.split(", "))

                            raise IncompleteRead(
                                partial=int(
                                    "".join(c for c in received if c.isdigit()).strip()
                                ),
                                expected=int(
                                    "".join(c for c in expected if c.isdigit()).strip()
                                ),
                            )
                    except (ValueError, IndexError):
                        pass

                    raise ProtocolError(event.message)
                elif isinstance(event, StreamResetReceived):
                    raise ProtocolError(
                        f"Stream {event.stream_id} was reset by remote peer. Reason: {hex(event.error_code)}."
                    )

                if data_in_len_from:
                    data_in_len += data_in_len_from(event)

                if not event_type_collectable:
                    events.append(event)
                else:
                    if isinstance(event, event_type_collectable):
                        events.append(event)
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
                        and hasattr(event, "end_stream")
                    ):
                        if event.end_stream is True:
                            if reshelve_events:
                                self._protocol.reshelve(*reshelve_events)
                            return events
                        continue

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

        self._start_last_request = datetime.now(tz=timezone.utc)

        if self._tunnel_host is not None:
            host, port = self._tunnel_host, self._tunnel_port
        else:
            host, port = self.host, self.port

        authority: bytes = host.encode("idna")

        self.__headers = [
            (b":method", method.encode("ascii")),
            (
                b":scheme",
                self.scheme.encode("ascii"),
            ),
            (b":path", url.encode("ascii")),
        ]

        if not skip_host:
            self.__headers.append(
                (
                    b":authority",
                    authority
                    if port == self.default_port  # type: ignore[attr-defined]
                    else authority + f":{port}".encode(),
                ),
            )

        if not skip_accept_encoding:
            self.putheader("Accept-Encoding", "identity")

    def putheader(self, header: str, *values: str) -> None:
        # note: requests allow passing headers as bytes (seen in requests/tests)
        # warn: always lowercase header names, quic transport crash if not lowercase.
        header = header.lower()

        encoded_header = header.encode("ascii") if isinstance(header, str) else header

        for value in values:
            self.__headers.append(
                (
                    encoded_header,
                    value.encode("iso-8859-1") if isinstance(value, str) else value,
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

        assert self.sock is not None
        assert self._protocol is not None

        # only h2 and h3 support streams, it is faked/simulated for h1.
        self._stream_id = self._protocol.get_available_stream_id()

        # unless anything hint the opposite, the request head frame is the end stream
        should_end_stream: bool = expect_body_afterward is False
        # only h11 support chunked transfer encoding, we internally translate
        # it to the right method for h2 and h3.
        support_te_chunked: bool = self._svn == HttpVersion.h11

        # determine if stream should end there (absent body case)
        for raw_header, raw_value in self.__headers:
            # Some programs does set value to None, and that is... an issue here. We ignore those key, value.
            if raw_value is None:
                continue
            header: str = raw_header.decode("ascii").lower().replace("_", "-")
            value: str = raw_value.decode("iso-8859-1")
            if header.startswith(":"):
                continue
            if header == "content-length":
                if value.isdigit():
                    self.__expected_body_length = int(value)
                break

        # handle cases where 'Host' header is set manually
        if any(k == b":authority" for k, v in self.__headers) is False:
            for raw_header, raw_value in self.__headers:
                header = raw_header.decode("ascii").lower().replace("_", "-")

                if header == "host":
                    self.__headers.append((b":authority", raw_value))
                    break
        if any(k == b":authority" for k, v in self.__headers) is False:
            raise ProtocolError(
                (
                    "urllib3.future do not support emitting HTTP requests without the `Host` header",
                    "It was only permitted in HTTP/1.0 and prior. This implementation ship with HTTP/1.1+.",
                )
            )

        if not support_te_chunked:
            # We MUST never use that header in h2 and h3 over quic.
            # It may(should) break the connection.
            try:
                self.__headers.remove((b"transfer-encoding", b"chunked"))
            except ValueError:
                pass

            # Passing 'Connection' header is actually a protocol violation above h11.
            # We assume it is passed as-is (meaning 'keep-alive' lower-cased)
            try:
                self.__headers.remove((b"connection", b"keep-alive"))
            except ValueError:
                pass

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
            self.sock.sendall(self._protocol.bytes_to_send())
        except BrokenPipeError as e:
            rp = ResponsePromise(self, self._stream_id, self.__headers)
            self._promises[rp.uid] = rp
            e.promise = rp  # type: ignore[attr-defined]

            raise e

        if should_end_stream:
            if self._start_last_request and self.conn_info:
                self.conn_info.request_sent_latency = (
                    datetime.now(tz=timezone.utc) - self._start_last_request
                )

            rp = ResponsePromise(self, self._stream_id, self.__headers)
            self._promises[rp.uid] = rp
            return rp

        return None

    def __read_st(
        self, __amt: int | None = None, __stream_id: int | None = None
    ) -> tuple[bytes, bool]:
        """Allows us to defer the body loading after constructing the response object."""
        eot = False

        events: list[DataReceived] = self.__exchange_until(  # type: ignore[assignment]
            DataReceived,
            receive_first=True,
            # we ignore Trailers even if provided in response.
            event_type_collectable=(DataReceived, HeadersReceived),
            maximal_data_in_read=__amt,
            data_in_len_from=lambda x: len(x.data)
            if isinstance(x, DataReceived)
            else 0,
            stream_id=__stream_id,
        )

        if events and events[-1].end_stream:
            eot = True

            if __stream_id in self._pending_responses:
                del self._pending_responses[__stream_id]

            if self.is_idle:
                # probe for h3/quic if available, and remember it.
                self._upgrade()

            # remote can refuse future inquiries, so no need to go further with this conn.
            if self._protocol and self._protocol.has_expired():
                self.close()

        return (
            b"".join(e.data for e in events if isinstance(e, DataReceived)),
            eot,
        )

    def getresponse(
        self, *, promise: ResponsePromise | None = None
    ) -> LowLevelResponse:
        if (
            self.sock is None
            or self._protocol is None
            or self._svn is None
            or not self._promises
        ):
            raise ResponseNotReady()  # Defensive: Comply with http.client, actually tested but not reported?

        headers = HTTPHeaderDict()
        status: int | None = None

        events: list[HeadersReceived] = self.__exchange_until(  # type: ignore[assignment]
            HeadersReceived,
            receive_first=True,
            event_type_collectable=(HeadersReceived,),
            respect_end_stream_signal=False,
            stream_id=promise.stream_id if promise else None,
        )

        for event in events:
            for raw_header, raw_value in event.headers:
                header: str = raw_header.decode("ascii")
                value: str = raw_value.decode("iso-8859-1")

                # special headers that represent (usually) the HTTP response status, version and reason.
                if header.startswith(":"):
                    if header == ":status" and value.isdigit():
                        status = int(value)
                        continue
                    # this should be unreachable.
                    # it is designed to detect eventual changes lower in the stack.
                    raise ProtocolError(  # Defensive:
                        f"Unhandled special header '{header}'"
                    )

                headers.add(header, value)

        if promise is None:
            for p in self._promises.values():
                if p.stream_id == events[-1].stream_id:
                    promise = p
                    break
            if promise is None:
                raise ProtocolError(
                    f"Response received (stream: {events[-1].stream_id}) but no promise in-flight"
                )

        # this should be unreachable
        if status is None:
            raise ProtocolError(  # Defensive: This is unreachable, all three implementations crash before.
                "Got an HTTP response without a status code. This is a violation."
            )

        eot = events[-1].end_stream is True

        response = LowLevelResponse(
            dict(promise.request_headers)[b":method"].decode("ascii"),
            status,
            self._http_vsn,
            responses[status] if status in responses else "Unknown",
            headers,
            self.__read_st if not eot else None,
            authority=self.host,
            port=self.port,
            stream_id=promise.stream_id,
        )

        promise.response = response
        response.from_promise = promise

        # we delivered a response, we can safely remove the promise from queue.
        del self._promises[promise.uid]

        # keep last response
        self._response: LowLevelResponse = response

        # save the quic ticket for session resumption
        if self._svn == HttpVersion.h3 and hasattr(self._protocol, "session_ticket"):
            self.__session_ticket = self._protocol.session_ticket

        if eot:
            if self.is_idle:
                self._upgrade()

            # remote can refuse future inquiries, so no need to go further with this conn.
            if self._protocol and self._protocol.has_expired():
                self.close()
        else:
            self._pending_responses[promise.stream_id] = response

        return response

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
                    if self._protocol.has_pending_event(stream_id=self._stream_id):
                        rp = ResponsePromise(self, self._stream_id, self.__headers)
                        self._promises[rp.uid] = rp

                        raise EarlyResponse(promise=rp)

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
                if remote_pipe_shutdown:
                    remote_pipe_shutdown.promise = rp  # type: ignore[attr-defined]
                    raise remote_pipe_shutdown
                return rp

        except self._protocol.exceptions() as e:
            raise ProtocolError(  # Defensive: In the unlikely event that exception may leak from below
                e
            ) from e

        return None

    def close(self) -> None:
        if self.sock:
            if self._protocol is not None:
                try:
                    self._protocol.submit_close()
                except self._protocol.exceptions() as e:  # Defensive:
                    # overly protective, made in case of possible exception leak.
                    raise ProtocolError(e) from e  # Defensive:
                else:
                    goodbye_trame: bytes = self._protocol.bytes_to_send()

                    if goodbye_trame:
                        self.sock.sendall(goodbye_trame)

            self.sock.close()

        self._protocol = None
        self._stream_id = None
        self._promises = {}
        self._pending_responses = {}
        self.__custom_tls_settings = None
        self.conn_info = None
        self.__expected_body_length = None
        self.__remaining_body_length = None
        self._start_last_request = None
