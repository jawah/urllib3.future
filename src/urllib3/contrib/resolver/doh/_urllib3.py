from __future__ import annotations

import socket
import typing
from base64 import b64encode
from collections import deque

from ...._collections import HTTPHeaderDict
from ....backend import ConnectionInfo, HttpVersion, ResponsePromise
from ....connectionpool import HTTPSConnectionPool
from ....response import HTTPResponse
from ....util.url import parse_url
from ..protocols import (
    BaseResolver,
    DomainNameServerQuery,
    DomainNameServerReturn,
    ProtocolResolver,
    SupportedQueryType,
)
from ..utils import is_ipv4, is_ipv6, validate_length_of


class HTTPSResolver(BaseResolver):
    """
    Advanced DNS over HTTPS resolver.
    No common ground emerged from IETF w/ JSON. Following Google’s DNS over HTTPS schematics that is
    also implemented at Cloudflare.

    Support RFC 8484 without JSON. Disabled by default.
    """

    implementation = "urllib3"
    protocol = ProtocolResolver.DOH

    def __init__(
        self,
        server: str | None,
        port: int | None = None,
        *patterns: str,
        **kwargs: typing.Any,
    ) -> None:
        super().__init__(server, port or 443, *patterns, **kwargs)

        self._path: str = "/resolve"

        if "path" in kwargs:
            if isinstance(kwargs["path"], str) and kwargs["path"] != "/":
                self._path = kwargs["path"]
            kwargs.pop("path")

        self._rfc8484: bool = False

        if "rfc8484" in kwargs:
            if kwargs["rfc8484"]:
                self._rfc8484 = True
            kwargs.pop("rfc8484")

        assert self._server is not None

        if "source_address" in kwargs:
            if isinstance(kwargs["source_address"], str):
                bind_ip, bind_port = kwargs["source_address"].split(":", 1)

                if bind_ip and bind_port.isdigit():
                    kwargs["source_address"] = (
                        bind_ip,
                        int(bind_port),
                    )
                else:
                    raise ValueError("invalid source_address given in parameters")
            else:
                raise ValueError("invalid source_address given in parameters")

        if "proxy" in kwargs:
            kwargs["_proxy"] = parse_url(kwargs["proxy"])
            kwargs.pop("proxy")

        if "maxsize" not in kwargs:
            kwargs["maxsize"] = 10

        if "proxy_headers" in kwargs and "_proxy" in kwargs:
            proxy_headers = HTTPHeaderDict()

            if not isinstance(kwargs["proxy_headers"], list):
                kwargs["proxy_headers"] = [kwargs["proxy_headers"]]

            for item in kwargs["proxy_headers"]:
                if ":" not in item:
                    raise ValueError("Passed header is invalid in DNS parameters")

                k, v = item.split(":", 1)
                proxy_headers.add(k, v)

            kwargs["_proxy_headers"] = proxy_headers

        if "headers" in kwargs:
            headers = HTTPHeaderDict()

            if not isinstance(kwargs["headers"], list):
                kwargs["headers"] = [kwargs["headers"]]

            for item in kwargs["headers"]:
                if ":" not in item:
                    raise ValueError("Passed header is invalid in DNS parameters")

                k, v = item.split(":", 1)
                headers.add(k, v)

            kwargs["headers"] = headers

        if "disabled_svn" in kwargs:
            if not isinstance(kwargs["disabled_svn"], list):
                kwargs["disabled_svn"] = [kwargs["disabled_svn"]]

            disabled_svn = set()

            for svn in kwargs["disabled_svn"]:
                svn = svn.lower()

                if svn == "h11":
                    disabled_svn.add(HttpVersion.h11)
                elif svn == "h2":
                    disabled_svn.add(HttpVersion.h2)
                elif svn == "h3":
                    disabled_svn.add(HttpVersion.h3)

            kwargs["disabled_svn"] = disabled_svn

        if "on_post_connection" in kwargs and callable(kwargs["on_post_connection"]):
            self._connection_callback: typing.Callable[
                [ConnectionInfo], None
            ] | None = kwargs["on_post_connection"]
            kwargs.pop("on_post_connection")
        else:
            self._connection_callback = None

        self._pool = HTTPSConnectionPool(self._server, self._port, **kwargs)
        self._unconsumed: deque[HTTPResponse] = deque()

    def close(self) -> None:
        self._pool.close()

    def is_available(self) -> bool:
        return self._pool.pool is not None

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
            raise socket.gaierror("Tried to resolve 'localhost' from a HTTPSResolver")

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

        promises: list[HTTPResponse | ResponsePromise] = []
        remote_preemptive_quic_rr = False

        if quic_upgrade_via_dns_rr and type == socket.SOCK_DGRAM:
            quic_upgrade_via_dns_rr = False

        if family in [socket.AF_UNSPEC, socket.AF_INET]:
            if not self._rfc8484:
                promises.append(
                    self._pool.request_encode_url(
                        "GET",
                        self._path,
                        {"name": host, "type": "1"},
                        headers={"Accept": "application/dns-json"},
                        on_post_connection=self._connection_callback,
                        multiplexed=True,
                    )
                )
            else:
                dns_query = DomainNameServerQuery(
                    host, SupportedQueryType.A, override_id=0
                )
                dns_payload = bytes(dns_query)
                promises.append(
                    self._pool.request_encode_url(
                        "GET",
                        self._path,
                        {
                            "dns": b64encode(dns_payload).decode().replace("=", ""),
                        },
                        headers={"Accept": "application/dns-message"},
                        on_post_connection=self._connection_callback,
                        multiplexed=True,
                    )
                )

        if family in [socket.AF_UNSPEC, socket.AF_INET6]:
            if not self._rfc8484:
                promises.append(
                    self._pool.request_encode_url(
                        "GET",
                        self._path,
                        {"name": host, "type": "28"},
                        headers={"Accept": "application/dns-json"},
                        on_post_connection=self._connection_callback,
                        multiplexed=True,
                    )
                )
            else:
                dns_query = DomainNameServerQuery(
                    host, SupportedQueryType.AAAA, override_id=0
                )
                dns_payload = bytes(dns_query)

                promises.append(
                    self._pool.request_encode_url(
                        "GET",
                        self._path,
                        {
                            "dns": b64encode(dns_payload).decode().replace("=", ""),
                        },
                        headers={"Accept": "application/dns-message"},
                        on_post_connection=self._connection_callback,
                        multiplexed=True,
                    )
                )

        if quic_upgrade_via_dns_rr:
            if not self._rfc8484:
                promises.append(
                    self._pool.request_encode_url(
                        "GET",
                        self._path,
                        {"name": host, "type": "65"},
                        headers={"Accept": "application/dns-json"},
                        on_post_connection=self._connection_callback,
                        multiplexed=True,
                    )
                )
            else:
                dns_query = DomainNameServerQuery(
                    host, SupportedQueryType.HTTPS, override_id=0
                )
                dns_payload = bytes(dns_query)

                promises.append(
                    self._pool.request_encode_url(
                        "GET",
                        self._path,
                        {
                            "dns": b64encode(dns_payload).decode().replace("=", ""),
                        },
                        headers={"Accept": "application/dns-message"},
                        on_post_connection=self._connection_callback,
                        multiplexed=True,
                    )
                )

        no_multiplexing: bool = isinstance(promises[0], HTTPResponse)

        # This edge case can happen when the initial request is emitted through HTTP/1.1
        # and a connection upgrade happen just after that (no multiplexing to multiplexing...)
        if (
            no_multiplexing
            and len(promises) > 1
            and isinstance(promises[1], HTTPResponse) is False
        ):
            force_resolve: list[HTTPResponse] = []

            for promise in promises:
                if isinstance(promise, HTTPResponse):
                    force_resolve.append(promise)
                    continue
                force_resolve.append(self._pool.get_response(promise=promise))  # type: ignore[arg-type]

            promises = force_resolve  # type: ignore[assignment]

        results: list[
            tuple[
                socket.AddressFamily,
                socket.SocketKind,
                int,
                str,
                tuple[str, int] | tuple[str, int, int, int],
            ]
        ] = []

        while promises:
            response = None

            if no_multiplexing is False:
                with self._lock:
                    if self._unconsumed:
                        for unconsumed in self._unconsumed:
                            for pending_promise in promises:
                                if unconsumed.is_from_promise(pending_promise):  # type: ignore[arg-type]
                                    response = unconsumed
                                    break
                            if response:
                                break
                        if response:
                            self._unconsumed.remove(response)

                    if not response:
                        response = self._pool.get_response()

                if response is None:
                    raise socket.gaierror(
                        "DNS over HTTPS failed due to a protocol error in urllib3.future or remote peer. Expected a response, got none."
                    )

                p = None

                for p in promises:
                    if response.is_from_promise(p):  # type: ignore[arg-type]
                        break

                if p is None:
                    with self._lock:
                        self._unconsumed.append(response)
                    continue

                promises.remove(p)
            else:
                response = promises.pop()  # type: ignore[assignment]

            assert response is not None

            if response.status >= 300:
                raise socket.gaierror(
                    f"DNS over HTTPS was unsuccessful, server response status {response.status}."
                )

            if not self._rfc8484:
                payload = response.json()

                assert "Status" in payload and isinstance(payload["Status"], int)

                if payload["Status"] != 0:
                    msg = (
                        payload["Comment"]
                        if "Comment" in payload
                        else f"Remote DNS indicated that an error occurred while providing resolution. Status {payload['Status']}."
                    )

                    if isinstance(msg, list):
                        msg = ", ".join(msg)

                    raise socket.gaierror(msg)

                assert "Question" in payload and isinstance(payload["Question"], list)

                if "Answer" not in payload:
                    continue

                assert isinstance(payload["Answer"], list)

                for answer in payload["Answer"]:
                    if answer["type"] not in [1, 28, 65]:
                        continue

                    assert "data" in answer
                    assert isinstance(answer["data"], str)

                    # DNS RR/HTTPS
                    if answer["type"] == 65:
                        # "1 . alpn=h3,h2 ipv4hint=104.16.132.229,104.16.133.229 ipv6hint=2606:4700::6810:84e5,2606:4700::6810:85e5"
                        # or..
                        # "1 . alpn=h2,h3"
                        rr: str = answer["data"]

                        if rr.startswith("\\#"):  # it means, raw, bytes.
                            rr = "".join(rr[2:].split(" ")[2:])

                            try:
                                raw_record = bytes.fromhex(rr)
                            except ValueError:
                                raw_record = b""

                            if b"h3" not in raw_record:
                                continue

                            remote_preemptive_quic_rr = True
                        else:
                            rr_decode: dict[str, str] = dict(
                                tuple(_.lower().split("=", 1))
                                for _ in rr.split(" ")
                                if "=" in _
                            )

                            if "alpn" not in rr_decode or "h3" not in rr_decode["alpn"]:
                                continue

                            remote_preemptive_quic_rr = True

                            if "ipv4hint" in rr_decode and family in [
                                socket.AF_UNSPEC,
                                socket.AF_INET,
                            ]:
                                for ipv4 in rr_decode["ipv4hint"].split(","):
                                    results.append(
                                        (
                                            socket.AF_INET,
                                            socket.SOCK_DGRAM,
                                            17,
                                            "",
                                            (
                                                ipv4,
                                                port,
                                            ),
                                        )
                                    )
                            if "ipv6hint" in rr_decode and family in [
                                socket.AF_UNSPEC,
                                socket.AF_INET6,
                            ]:
                                for ipv6 in rr_decode["ipv6hint"].split(","):
                                    results.append(
                                        (
                                            socket.AF_INET6,
                                            socket.SOCK_DGRAM,
                                            17,
                                            "",
                                            (
                                                ipv6,
                                                port,
                                                0,
                                                0,
                                            ),
                                        )
                                    )

                        continue

                    inet_type = (
                        socket.AF_INET if answer["type"] == 1 else socket.AF_INET6
                    )

                    dst_addr: tuple[str, int] | tuple[str, int, int, int] = (
                        (
                            answer["data"],
                            port,
                        )
                        if inet_type == socket.AF_INET
                        else (
                            answer["data"],
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
            else:
                dns_resp = DomainNameServerReturn(response.data)

                for record in dns_resp.records:
                    if record[0] == SupportedQueryType.HTTPS:
                        if "h3" in record[-1]:
                            remote_preemptive_quic_rr = True
                        continue

                    inet_type = (
                        socket.AF_INET
                        if record[0] == SupportedQueryType.A
                        else socket.AF_INET6
                    )
                    dst_addr = (
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

        quic_results: list[
            tuple[
                socket.AddressFamily,
                socket.SocketKind,
                int,
                str,
                tuple[str, int] | tuple[str, int, int, int],
            ]
        ] = []

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


class GoogleResolver(
    HTTPSResolver
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
        if "rfc8484" in kwargs:
            if kwargs["rfc8484"]:
                kwargs["path"] = "/dns-query"
        super().__init__("dns.google", port, *patterns, **kwargs)


class CloudflareResolver(
    HTTPSResolver
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

        kwargs.update({"path": "/dns-query"})
        super().__init__("cloudflare-dns.com", port, *patterns, **kwargs)


class AdGuardResolver(
    HTTPSResolver
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

        kwargs.update({"path": "/dns-query", "rfc8484": True})
        super().__init__("unfiltered.adguard-dns.com", port, *patterns, **kwargs)


class OpenDNSResolver(
    HTTPSResolver
):  # Defensive: we do not cover specific vendors/DNS shortcut
    specifier = "opendns"

    def __init__(self, *patterns: str, **kwargs: typing.Any) -> None:
        if "server" in kwargs:
            kwargs.pop("server")
        if "port" in kwargs:
            port = kwargs["port"]
            kwargs.pop("port")
        else:
            port = None

        kwargs.update({"path": "/dns-query", "rfc8484": True})
        super().__init__("dns.opendns.com", port, *patterns, **kwargs)


class Quad9Resolver(
    HTTPSResolver
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

        kwargs.update({"path": "/dns-query", "rfc8484": True})
        super().__init__("dns11.quad9.net", port, *patterns, **kwargs)


class NextDNSResolver(
    HTTPSResolver
):  # Defensive: we do not cover specific vendors/DNS shortcut
    specifier = "nextdns"

    def __init__(self, *patterns: str, **kwargs: typing.Any) -> None:
        if "server" in kwargs:
            kwargs.pop("server")
        if "port" in kwargs:
            port = kwargs["port"]
            kwargs.pop("port")
        else:
            port = None

        super().__init__("dns.nextdns.io", port, *patterns, **kwargs)
