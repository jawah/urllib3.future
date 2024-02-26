from __future__ import annotations

import asyncio
import socket
import ssl
import struct
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

from .....util.ssl_ import resolve_cert_reqs
from ...protocols import (
    COMMON_RCODE_LABEL,
    DomainNameServerQuery,
    DomainNameServerReturn,
    ProtocolResolver,
    SupportedQueryType,
)
from ...utils import is_ipv4, is_ipv6, packet_fragment, validate_length_of
from ..dou import PlainResolver


class QUICResolver(PlainResolver):
    protocol = ProtocolResolver.DOQ
    implementation = "qh3"

    def __init__(
        self,
        server: str | None,
        port: int | None = None,
        *patterns: str,
        **kwargs: typing.Any,
    ):
        super().__init__(server, port or 853, *patterns, **kwargs)

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

        self._read_semaphore: asyncio.Semaphore = asyncio.Semaphore()
        self._handshake_event: asyncio.Event = asyncio.Event()

        self._terminated: bool = False
        self._should_disconnect: bool = False

        # DNS over QUIC mandate the size-prefix (unsigned int, 2b)
        self._hook_in = lambda p: p[2:]
        self._hook_out = lambda p: struct.pack("!H", len(p)) + p

        self._unconsumed: deque[DomainNameServerReturn] = deque()
        self._pending: deque[DomainNameServerQuery] = deque()

    async def close(self) -> None:  # type: ignore[override]
        if not self._terminated and not self._socket.should_connect():
            self._quic.close()

            while True:
                datagrams = self._quic.datagrams_to_send(monotonic())

                if not datagrams:
                    break

                for datagram in datagrams:
                    data, addr = datagram
                    await self._socket.sendall(data)

            self._socket.close()
            self._terminated = True
        if self._socket.should_connect():
            self._terminated = True

    def is_available(self) -> bool:
        if self._terminated:
            return False
        if self._socket.should_connect():
            return True
        self._quic.handle_timer(monotonic())
        if hasattr(self._quic, "_close_event") and self._quic._close_event is not None:
            self._terminated = True
        return not self._terminated

    async def getaddrinfo(  # type: ignore[override]
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
            raise socket.gaierror("Tried to resolve 'localhost' using the QUICResolver")
        if port is None:
            port = 0
        if isinstance(port, str):
            port = int(port)
        if port < 0:
            raise socket.gaierror("Servname not supported for ai_socktype")

        if isinstance(host, bytes):
            host = host.decode("ascii")

        if is_ipv4(host):
            if family == socket.AF_INET6:
                raise socket.gaierror("Address family for hostname not supported")
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
                raise socket.gaierror("Address family for hostname not supported")
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

        if self._socket.should_connect():
            assert self.server is not None
            self._quic.connect((self._server, self._port), monotonic())
            await self._socket.connect((self.server, self.port or 853))
            await self.__exchange_until(HandshakeCompleted, receive_first=False)
            self._handshake_event.set()
        else:
            await self._handshake_event.wait()

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

        for q in queries:
            payload = bytes(q)

            self._pending.append(q)

            if self._hook_out is not None:
                payload = self._hook_out(payload)

            stream_id = self._quic.get_next_available_stream_id()
            self._quic.send_stream_data(stream_id, payload, True)

            open_streams.append(stream_id)

            for dg in self._quic.datagrams_to_send(monotonic()):
                await self._socket.sendall(dg[0])

        responses: list[DomainNameServerReturn] = []

        while len(responses) < len(tbq):
            await self._read_semaphore.acquire()
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
                    self._read_semaphore.release()
                    continue

            events: list[StreamDataReceived] = await self.__exchange_until(  # type: ignore[assignment]
                StreamDataReceived,
                receive_first=True,
                event_type_collectable=(StreamDataReceived,),
            )

            payload = b"".join([e.data for e in events])

            self._read_semaphore.release()

            if not payload:
                continue

            pending_raw_identifiers = [_.raw_id for _ in self._pending]

            #: We can receive two responses at once (or more, concatenated). Let's unwrap them.
            fragments = packet_fragment(payload, *pending_raw_identifiers)

            for fragment in fragments:
                if self._hook_in is not None:
                    fragment = self._hook_in(fragment)

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
            await self.close()
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
                    if "h3" in record[-1]:
                        remote_preemptive_quic_rr = True
                    continue

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

    async def __exchange_until(
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
                        await self._socket.sendall(data)

            events = []

            while True:
                data = await self._socket.recv(1500)

                if not data:
                    break

                now = monotonic()

                self._quic.receive_datagram(data, (self._server, self._port), now)

                while True:
                    datagrams = self._quic.datagrams_to_send(now)

                    if not datagrams:
                        break

                    for datagram in datagrams:
                        data, addr = datagram
                        await self._socket.sendall(data)

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


class AdGuardResolver(QUICResolver):
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


class NextDNSResolver(QUICResolver):
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
