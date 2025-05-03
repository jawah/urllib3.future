from __future__ import annotations

import socket
import ssl
import typing
from collections import deque
from ssl import SSLError
from time import time as monotonic

from qh3.quic.configuration import QuicConfiguration
from qh3.quic.connection import QuicConnection
from qh3.quic.events import (
    ConnectionTerminated,
    HandshakeCompleted,
    QuicEvent,
    StopSendingReceived,
    StreamDataReceived,
    StreamReset,
)

from ....util.ssl_ import IS_FIPS, resolve_cert_reqs
from ..dou import PlainResolver
from ..protocols import (
    COMMON_RCODE_LABEL,
    DomainNameServerQuery,
    DomainNameServerReturn,
    ProtocolResolver,
    SupportedQueryType,
)
from ..utils import (
    is_ipv4,
    is_ipv6,
    rfc1035_pack,
    rfc1035_should_read,
    rfc1035_unpack,
    validate_length_of,
)

if IS_FIPS:
    raise ImportError(
        "DNS-over-QUIC disabled when Python is built with FIPS-compliant ssl module"
    )


class QUICResolver(PlainResolver):
    protocol = ProtocolResolver.DOQ
    implementation = "qh3"

    def __init__(
        self,
        server: str,
        port: int | None = None,
        *patterns: str,
        **kwargs: typing.Any,
    ):
        super().__init__(server, port or 853, *patterns, **kwargs)

        # qh3 load_default_certs seems off. need to investigate.
        if "ca_cert_data" not in kwargs and "ca_certs" not in kwargs:
            kwargs["ca_cert_data"] = []

            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

            try:
                ctx.load_default_certs()

                for der in ctx.get_ca_certs(binary_form=True):
                    kwargs["ca_cert_data"].append(ssl.DER_cert_to_PEM_cert(der))

                if kwargs["ca_cert_data"]:
                    kwargs["ca_cert_data"] = "".join(kwargs["ca_cert_data"])
                else:
                    del kwargs["ca_cert_data"]
            except (AttributeError, ValueError, OSError):
                del kwargs["ca_cert_data"]

        if "ca_cert_data" not in kwargs and "ca_certs" not in kwargs:
            if (
                "cert_reqs" not in kwargs
                or resolve_cert_reqs(kwargs["cert_reqs"]) is ssl.CERT_REQUIRED
            ):
                raise ssl.SSLError(
                    "DoQ requires at least one CA loaded in order to verify the remote peer certificate. "
                    "Add ?cert_reqs=0 to disable certificate checks."
                )

        configuration = QuicConfiguration(
            is_client=True,
            alpn_protocols=["doq"],
            server_name=self._server
            if "server_hostname" not in kwargs
            else kwargs["server_hostname"],
            verify_mode=resolve_cert_reqs(kwargs["cert_reqs"])
            if "cert_reqs" in kwargs
            else ssl.CERT_REQUIRED,
            cadata=kwargs["ca_cert_data"].encode()
            if "ca_cert_data" in kwargs
            else None,
            cafile=kwargs["ca_certs"] if "ca_certs" in kwargs else None,
            idle_timeout=300.0,
        )

        if "cert_file" in kwargs:
            configuration.load_cert_chain(
                kwargs["cert_file"],
                kwargs["key_file"] if "key_file" in kwargs else None,
                kwargs["key_password"] if "key_password" in kwargs else None,
            )
        elif "cert_data" in kwargs:
            configuration.load_cert_chain(
                kwargs["cert_data"],
                kwargs["key_data"] if "key_data" in kwargs else None,
                kwargs["key_password"] if "key_password" in kwargs else None,
            )

        self._quic = QuicConnection(configuration=configuration)

        self._quic.connect((self._server, self._port), monotonic())
        self.__exchange_until(HandshakeCompleted, receive_first=False)

        self._terminated: bool = False
        self._should_disconnect: bool = False

        # DNS over QUIC mandate the size-prefix (unsigned int, 2b)
        self._rfc1035_prefix_mandated = True

        self._unconsumed: deque[DomainNameServerReturn] = deque()
        self._pending: deque[DomainNameServerQuery] = deque()

    def close(self) -> None:
        if not self._terminated:
            with self._lock:
                self._quic.close()

                while True:
                    datagrams = self._quic.datagrams_to_send(monotonic())

                    if not datagrams:
                        break

                    for datagram in datagrams:
                        data, addr = datagram
                        self._socket.sendall(data)

                self._socket.close()
                self._terminated = True

    def is_available(self) -> bool:
        self._quic.handle_timer(monotonic())
        if hasattr(self._quic, "_close_event") and self._quic._close_event is not None:
            self._terminated = True
        return not self._terminated

    def getaddrinfo(
        self,
        host: bytes | str | None,
        port: str | int | None,
        family: socket.AddressFamily,
        type: socket.SocketKind,
        proto: int = 0,
        flags: int = 0,
        *,
        quic_upgrade_via_dns_rr: bool = False,
    ) -> list[
        tuple[
            socket.AddressFamily,
            socket.SocketKind,
            int,
            str,
            tuple[str, int] | tuple[str, int, int, int],
        ]
    ]:
        if host is None:
            raise socket.gaierror(  # Defensive: stdlib cpy behavior
                "Tried to resolve 'localhost' using the QUICResolver"
            )
        if port is None:
            port = 0  # Defensive: stdlib cpy behavior
        if isinstance(port, str):
            port = int(port)  # Defensive: stdlib cpy behavior
        if port < 0:
            raise socket.gaierror(  # Defensive: stdlib cpy behavior
                "Servname not supported for ai_socktype"
            )

        if isinstance(host, bytes):
            host = host.decode("ascii")  # Defensive: stdlib cpy behavior

        if is_ipv4(host):
            if family == socket.AF_INET6:
                raise socket.gaierror(  # Defensive: stdlib cpy behavior
                    "Address family for hostname not supported"
                )
            return [
                (
                    socket.AF_INET,
                    type,
                    6,
                    "",
                    (
                        host,
                        port,
                    ),
                )
            ]
        elif is_ipv6(host):
            if family == socket.AF_INET:
                raise socket.gaierror(  # Defensive: stdlib cpy behavior
                    "Address family for hostname not supported"
                )
            return [
                (
                    socket.AF_INET6,
                    type,
                    17,
                    "",
                    (
                        host,
                        port,
                        0,
                        0,
                    ),
                )
            ]

        validate_length_of(host)

        remote_preemptive_quic_rr = False

        if quic_upgrade_via_dns_rr and type == socket.SOCK_DGRAM:
            quic_upgrade_via_dns_rr = False

        tbq = []

        if family in [socket.AF_UNSPEC, socket.AF_INET]:
            tbq.append(SupportedQueryType.A)

        if family in [socket.AF_UNSPEC, socket.AF_INET6]:
            tbq.append(SupportedQueryType.AAAA)

        if quic_upgrade_via_dns_rr:
            tbq.append(SupportedQueryType.HTTPS)

        queries = DomainNameServerQuery.bulk(host, *tbq)
        open_streams = []

        with self._lock:
            for q in queries:
                payload = bytes(q)

                self._pending.append(q)

                if self._rfc1035_prefix_mandated is True:
                    payload = rfc1035_pack(payload)

                stream_id = self._quic.get_next_available_stream_id()
                self._quic.send_stream_data(stream_id, payload, True)

                open_streams.append(stream_id)

                for dg in self._quic.datagrams_to_send(monotonic()):
                    self._socket.sendall(dg[0])

        responses: list[DomainNameServerReturn] = []

        while len(responses) < len(tbq):
            with self._lock:
                if self._unconsumed:
                    dns_resp = None
                    for query in queries:
                        for unconsumed in self._unconsumed:
                            if unconsumed.id == query.id:
                                dns_resp = unconsumed
                                responses.append(dns_resp)
                                break
                        if dns_resp:
                            break
                    if dns_resp:
                        self._unconsumed.remove(dns_resp)
                        self._pending.remove(query)
                        continue

                try:
                    events: list[StreamDataReceived] = self.__exchange_until(  # type: ignore[assignment]
                        StreamDataReceived,
                        receive_first=True,
                        event_type_collectable=(StreamDataReceived,),
                        respect_end_stream_signal=False,
                    )

                    payload = b"".join([e.data for e in events])

                    while rfc1035_should_read(payload):
                        events.extend(
                            self.__exchange_until(  # type: ignore[arg-type]
                                StreamDataReceived,
                                receive_first=True,
                                event_type_collectable=(StreamDataReceived,),
                                respect_end_stream_signal=False,
                            )
                        )
                        payload = b"".join([e.data for e in events])
                except (TimeoutError, OSError, socket.timeout, ConnectionError) as e:
                    raise socket.gaierror(
                        "Got unexpectedly disconnected while waiting for name resolution"
                    ) from e

                if not payload:
                    continue

                #: We can receive two responses at once (or more, concatenated). Let's unwrap them.
                fragments = rfc1035_unpack(payload)

                for fragment in fragments:
                    dns_resp = DomainNameServerReturn(fragment)

                    if any(dns_resp.id == _.id for _ in queries):
                        responses.append(dns_resp)

                        query_tbr: DomainNameServerQuery | None = None

                        for query_tbr in self._pending:
                            if query_tbr.id == dns_resp.id:
                                break
                        if query_tbr:
                            self._pending.remove(query_tbr)
                    else:
                        self._unconsumed.append(dns_resp)

        if self._should_disconnect:
            with self._lock:
                self.close()
                self._should_disconnect = False
                self._terminated = True

        results = []

        for response in responses:
            if not response.is_ok:
                if response.rcode == 2:
                    raise socket.gaierror(
                        f"DNSSEC validation failure. Check http://dnsviz.net/d/{host}/dnssec/ and http://dnssec-debugger.verisignlabs.com/{host} for errors"
                    )
                raise socket.gaierror(
                    f"DNS returned an error: {COMMON_RCODE_LABEL[response.rcode] if response.rcode in COMMON_RCODE_LABEL else f'code {response.rcode}'}"
                )

            for record in response.records:
                if record[0] == SupportedQueryType.HTTPS:
                    assert isinstance(record[-1], dict)
                    if "h3" in record[-1]["alpn"]:
                        remote_preemptive_quic_rr = True
                    continue

                assert not isinstance(record[-1], dict)

                inet_type = (
                    socket.AF_INET
                    if record[0] == SupportedQueryType.A
                    else socket.AF_INET6
                )
                dst_addr: tuple[str, int] | tuple[str, int, int, int] = (
                    (
                        record[-1],
                        port,
                    )
                    if inet_type == socket.AF_INET
                    else (
                        record[-1],
                        port,
                        0,
                        0,
                    )
                )

                results.append(
                    (
                        inet_type,
                        type,
                        6 if type == socket.SOCK_STREAM else 17,
                        "",
                        dst_addr,
                    )
                )

        quic_results = []

        if remote_preemptive_quic_rr:
            any_specified = False

            for result in results:
                if result[1] == socket.SOCK_STREAM:
                    quic_results.append(
                        (result[0], socket.SOCK_DGRAM, 17, "", result[4])
                    )
                else:
                    any_specified = True
                    break

            if any_specified:
                quic_results = []

        return sorted(quic_results + results, key=lambda _: _[0] + _[1], reverse=True)

    def __exchange_until(
        self,
        event_type: type[QuicEvent] | tuple[type[QuicEvent], ...],
        *,
        receive_first: bool = False,
        event_type_collectable: type[QuicEvent]
        | tuple[type[QuicEvent], ...]
        | None = None,
        respect_end_stream_signal: bool = True,
    ) -> list[QuicEvent]:
        while True:
            if receive_first is False:
                now = monotonic()
                while True:
                    datagrams = self._quic.datagrams_to_send(now)

                    if not datagrams:
                        break

                    for datagram in datagrams:
                        data, addr = datagram
                        self._socket.sendall(data)

            events = []

            while True:
                if not self._quic._events:
                    data = self._socket.recv(1500)

                    if not data:
                        break

                    now = monotonic()

                    self._quic.receive_datagram(data, (self._server, self._port), now)

                    while True:
                        now = monotonic()
                        datagrams = self._quic.datagrams_to_send(now)

                        if not datagrams:
                            break

                        for datagram in datagrams:
                            data, addr = datagram
                            self._socket.sendall(data)

                for ev in iter(self._quic.next_event, None):
                    if isinstance(ev, ConnectionTerminated):
                        if ev.error_code == 298:
                            raise SSLError(
                                "DNS over QUIC did not succeed (Error 298). Chain certificate verification failed."
                            )
                        raise socket.gaierror(
                            f"DNS over QUIC encountered a unrecoverable failure (error {ev.error_code} {ev.reason_phrase})"
                        )
                    elif isinstance(ev, StreamReset):
                        self._terminated = True
                        raise socket.gaierror(
                            "DNS over QUIC server submitted a StreamReset. A request was rejected."
                        )
                    elif isinstance(ev, StopSendingReceived):
                        self._should_disconnect = True
                        continue

                    if event_type_collectable:
                        if isinstance(ev, event_type_collectable):
                            events.append(ev)
                    else:
                        events.append(ev)

                    if isinstance(ev, event_type):
                        if not respect_end_stream_signal:
                            return events
                        if hasattr(ev, "stream_ended") and ev.stream_ended:
                            return events
                        elif hasattr(ev, "stream_ended") is False:
                            return events

            return events


class AdGuardResolver(
    QUICResolver
):  # Defensive: we do not cover specific vendors/DNS shortcut
    specifier = "adguard"

    def __init__(self, *patterns: str, **kwargs: typing.Any):
        if "server" in kwargs:
            kwargs.pop("server")
        if "port" in kwargs:
            port = kwargs["port"]
            kwargs.pop("port")
        else:
            port = None
        super().__init__("unfiltered.adguard-dns.com", port, *patterns, **kwargs)


class NextDNSResolver(
    QUICResolver
):  # Defensive: we do not cover specific vendors/DNS shortcut
    specifier = "nextdns"

    def __init__(self, *patterns: str, **kwargs: typing.Any):
        if "server" in kwargs:
            kwargs.pop("server")
        if "port" in kwargs:
            port = kwargs["port"]
            kwargs.pop("port")
        else:
            port = None
        super().__init__("dns.nextdns.io", port, *patterns, **kwargs)
