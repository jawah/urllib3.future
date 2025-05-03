from __future__ import annotations

import socket
import typing
from collections import deque

from ..protocols import (
    COMMON_RCODE_LABEL,
    BaseResolver,
    DomainNameServerQuery,
    DomainNameServerReturn,
    ProtocolResolver,
    SupportedQueryType,
)
from ..system import SystemResolver
from ..utils import (
    is_ipv4,
    is_ipv6,
    packet_fragment,
    rfc1035_pack,
    rfc1035_should_read,
    rfc1035_unpack,
    validate_length_of,
)


class PlainResolver(BaseResolver):
    """
    Minimalist DNS resolver over UDP
    Comply with RFC 1035: https://datatracker.ietf.org/doc/html/rfc1035

    EDNS is not supported, yet. But we plan to. Willing to contribute?
    """

    protocol = ProtocolResolver.DOU
    implementation = "socket"

    def __init__(
        self,
        server: str,
        port: int | None = None,
        *patterns: str,
        **kwargs: typing.Any,
    ) -> None:
        super().__init__(server, port, *patterns, **kwargs)

        if not hasattr(self, "_socket"):
            if "timeout" in kwargs and isinstance(
                kwargs["timeout"],
                (
                    float,
                    int,
                ),
            ):
                timeout = kwargs["timeout"]
            else:
                timeout = None

            if "source_address" in kwargs and isinstance(kwargs["source_address"], str):
                bind_ip, bind_port = kwargs["source_address"].split(":", 1)
            else:
                bind_ip, bind_port = "0.0.0.0", "0"

            self._socket = SystemResolver().create_connection(
                (server, port or 53),
                timeout=timeout,
                source_address=(bind_ip, int(bind_port))
                if bind_ip != "0.0.0.0" or bind_port != "0"
                else None,
                socket_options=None,
                socket_kind=socket.SOCK_DGRAM,
            )

        #: Only useful for inheritance, e.g. DNS over TLS support dns-message but require a prefix.
        self._rfc1035_prefix_mandated: bool = False

        self._unconsumed: deque[DomainNameServerReturn] = deque()
        self._pending: deque[DomainNameServerQuery] = deque()

        self._terminated: bool = False

    def close(self) -> None:
        if not self._terminated:
            with self._lock:
                if self._socket is not None:
                    self._socket.shutdown(0)
                    self._socket.close()
                self._terminated = True

    def is_available(self) -> bool:
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
                "Tried to resolve 'localhost' from a PlainResolver"
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

        with self._lock:
            for q in queries:
                payload = bytes(q)
                self._pending.append(q)

                if self._rfc1035_prefix_mandated is True:
                    payload = rfc1035_pack(payload)

                self._socket.sendall(payload)

        responses: list[DomainNameServerReturn] = []

        while len(responses) < len(tbq):
            with self._lock:
                #: There we want to verify if another thread got a response that belong to this thread.
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
                        self._pending.remove(query)
                        self._unconsumed.remove(dns_resp)
                        continue

                try:
                    payload = self._socket.recv(1500)

                    if self._rfc1035_prefix_mandated is True:
                        while rfc1035_should_read(payload):
                            payload += self._socket.recv(1500)
                except (TimeoutError, OSError, socket.timeout, ConnectionError) as e:
                    raise socket.gaierror(
                        "Got unexpectedly disconnected while waiting for name resolution"
                    ) from e

                if not payload:
                    self._terminated = True
                    raise socket.gaierror(
                        "Got unexpectedly disconnected while waiting for name resolution"
                    )

                pending_raw_identifiers = [_.raw_id for _ in self._pending]

                #: We can receive two responses at once (or more, concatenated). Let's unwrap them.
                if self._rfc1035_prefix_mandated is True:
                    fragments = rfc1035_unpack(payload)
                else:
                    fragments = packet_fragment(payload, *pending_raw_identifiers)

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


class CloudflareResolver(
    PlainResolver
):  # Defensive: we do not cover specific vendors/DNS shortcut
    specifier = "cloudflare"

    def __init__(self, *patterns: str, **kwargs: typing.Any) -> None:
        if "server" in kwargs:
            kwargs.pop("server")
        if "port" in kwargs:
            port = kwargs["port"]
            kwargs.pop("port")
        else:
            port = None

        super().__init__("1.1.1.1", port, *patterns, **kwargs)


class GoogleResolver(
    PlainResolver
):  # Defensive: we do not cover specific vendors/DNS shortcut
    specifier = "google"

    def __init__(self, *patterns: str, **kwargs: typing.Any) -> None:
        if "server" in kwargs:
            kwargs.pop("server")
        if "port" in kwargs:
            port = kwargs["port"]
            kwargs.pop("port")
        else:
            port = None

        super().__init__("8.8.8.8", port, *patterns, **kwargs)


class Quad9Resolver(
    PlainResolver
):  # Defensive: we do not cover specific vendors/DNS shortcut
    specifier = "quad9"

    def __init__(self, *patterns: str, **kwargs: typing.Any) -> None:
        if "server" in kwargs:
            kwargs.pop("server")
        if "port" in kwargs:
            port = kwargs["port"]
            kwargs.pop("port")
        else:
            port = None

        super().__init__("9.9.9.9", port, *patterns, **kwargs)


class AdGuardResolver(
    PlainResolver
):  # Defensive: we do not cover specific vendors/DNS shortcut
    specifier = "adguard"

    def __init__(self, *patterns: str, **kwargs: typing.Any) -> None:
        if "server" in kwargs:
            kwargs.pop("server")
        if "port" in kwargs:
            port = kwargs["port"]
            kwargs.pop("port")
        else:
            port = None

        super().__init__("94.140.14.140", port, *patterns, **kwargs)
