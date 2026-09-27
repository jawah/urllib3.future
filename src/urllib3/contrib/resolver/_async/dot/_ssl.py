from __future__ import annotations

import socket
import typing

from .....util._async.ssl_ import ssl_wrap_socket
from .....util.ssl_ import resolve_cert_reqs
from ...protocols import (
    DomainNameServerParseException,
    DomainNameServerQuery,
    DomainNameServerReturn,
    ProtocolResolver,
)
from ...utils import rfc1035_pack, rfc1035_should_read, rfc1035_unpack
from ..dou import PlainResolver
from ..system import SystemResolver


class TLSResolver(PlainResolver):
    """
    Basic DNS resolver over TLS.
    Comply with RFC 7858: https://datatracker.ietf.org/doc/html/rfc7858
    """

    protocol = ProtocolResolver.DOT
    implementation = "ssl"

    def __init__(
        self,
        server: str | None,
        port: int | None = None,
        *patterns: str,
        **kwargs: typing.Any,
    ) -> None:
        self._socket_type = socket.SOCK_STREAM

        super().__init__(server, port or 853, *patterns, **kwargs)

        # DNS over TLS mandate the size-prefix (unsigned int, 2 bytes)
        self._rfc1035_prefix_mandated = True

    async def _connect(self) -> None:
        assert self.server is not None
        raw_socket = await SystemResolver().create_connection(
            (self.server, self.port or 853),
            timeout=self._timeout,
            source_address=self._source_address,
            socket_options=((socket.IPPROTO_TCP, socket.TCP_NODELAY, 1, "tcp"),),
            socket_kind=self._socket_type,
        )
        try:
            self._socket = await ssl_wrap_socket(
                raw_socket,
                server_hostname=(
                    self.server
                    if "server_hostname" not in self._kwargs
                    else self._kwargs["server_hostname"]
                ),
                keyfile=self._kwargs.get("key_file"),
                certfile=self._kwargs.get("cert_file"),
                cert_reqs=(
                    resolve_cert_reqs(self._kwargs["cert_reqs"])
                    if "cert_reqs" in self._kwargs
                    else None
                ),
                ca_certs=self._kwargs.get("ca_certs"),
                ssl_version=self._kwargs.get("ssl_version"),
                ciphers=self._kwargs.get("ciphers"),
                ca_cert_dir=self._kwargs.get("ca_cert_dir"),
                key_password=self._kwargs.get("key_password"),
                ca_cert_data=self._kwargs.get("ca_cert_data"),
                certdata=self._kwargs.get("cert_data"),
                keydata=self._kwargs.get("key_data"),
            )
        except BaseException:
            raw_socket.close()
            await raw_socket.wait_for_close()
            raise

    async def _exchange(
        self, queries: list[DomainNameServerQuery], deadline: float | None
    ) -> list[DomainNameServerReturn]:
        assert self._socket is not None
        for query in queries:
            payload = bytes(query)
            if self._rfc1035_prefix_mandated is True:
                payload = rfc1035_pack(payload)
            await self._socket.sendall(payload)

        responses: list[DomainNameServerReturn] = []
        response_ids: set[int] = set()
        while len(responses) < len(queries):
            async with self._read_semaphore:
                with self._lock:
                    for query in queries:
                        dns_resp = self._completed.get(query.id)
                        if dns_resp is not None and query.id not in response_ids:
                            responses.append(dns_resp)
                            response_ids.add(query.id)
                if len(responses) == len(queries):
                    continue

                try:
                    data_in_or_segments = await self._socket.recv(1500)

                    if isinstance(data_in_or_segments, list):
                        payloads = data_in_or_segments
                    elif data_in_or_segments:
                        payloads = [data_in_or_segments]
                    else:
                        payloads = []

                    if self._rfc1035_prefix_mandated is True and payloads:
                        payload = b"".join(payloads)
                        while rfc1035_should_read(payload):
                            extra = await self._socket.recv(1500)
                            if not extra:
                                payloads = []
                                break
                            if isinstance(extra, list):
                                payload += b"".join(extra)
                            else:
                                payload += extra
                        else:
                            payloads = [payload]
                except (
                    TimeoutError,
                    OSError,
                    socket.timeout,
                    ConnectionError,
                ) as e:
                    raise socket.gaierror(
                        "Got unexpectedly disconnected while waiting for name resolution"
                    ) from e

                if not payloads:
                    await self.close()
                    raise socket.gaierror(
                        "DNS server closed the connection before sending a complete response"
                    )

                for payload in payloads:
                    if self._rfc1035_prefix_mandated is True:
                        fragments = rfc1035_unpack(payload)
                    else:
                        fragments = (payload,)

                    for fragment in fragments:
                        try:
                            dns_resp = DomainNameServerReturn(fragment)
                        except DomainNameServerParseException:
                            continue
                        with self._lock:
                            pending_query = self._pending.get(dns_resp.id)
                            if (
                                pending_query is not None
                                and dns_resp.matches(pending_query)
                                and dns_resp.id not in self._completed
                            ):
                                self._completed[dns_resp.id] = dns_resp
        return responses


class GoogleResolver(
    TLSResolver
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

        super().__init__("dns.google", port, *patterns, **kwargs)


class CloudflareResolver(
    TLSResolver
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


class AdGuardResolver(
    TLSResolver
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

        super().__init__("unfiltered.adguard-dns.com", port, *patterns, **kwargs)


class OpenDNSResolver(
    TLSResolver
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

        super().__init__("dns.opendns.com", port, *patterns, **kwargs)


class Quad9Resolver(
    TLSResolver
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

        super().__init__("dns11.quad9.net", port, *patterns, **kwargs)
