from __future__ import annotations

import socket
import typing

from .....util._async.ssl_ import ssl_wrap_socket
from .....util.ssl_ import resolve_cert_reqs
from ...protocols import ProtocolResolver
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
        if self._socket is None and self._connect_attempt.is_set() is False:
            assert self.server is not None
            self._connect_attempt.set()
            self._socket = await SystemResolver().create_connection(
                (self.server, self.port or 853),
                timeout=self._timeout,
                source_address=self._source_address,
                socket_options=((socket.IPPROTO_TCP, socket.TCP_NODELAY, 1, "tcp"),),
                socket_kind=self._socket_type,
            )
            self._socket = await ssl_wrap_socket(
                self._socket,
                server_hostname=self.server
                if "server_hostname" not in self._kwargs
                else self._kwargs["server_hostname"],
                keyfile=self._kwargs["key_file"]
                if "key_file" in self._kwargs
                else None,
                certfile=self._kwargs["cert_file"]
                if "cert_file" in self._kwargs
                else None,
                cert_reqs=resolve_cert_reqs(self._kwargs["cert_reqs"])
                if "cert_reqs" in self._kwargs
                else None,
                ca_certs=self._kwargs["ca_certs"]
                if "ca_certs" in self._kwargs
                else None,
                ssl_version=self._kwargs["ssl_version"]
                if "ssl_version" in self._kwargs
                else None,
                ciphers=self._kwargs["ciphers"] if "ciphers" in self._kwargs else None,
                ca_cert_dir=self._kwargs["ca_cert_dir"]
                if "ca_cert_dir" in self._kwargs
                else None,
                key_password=self._kwargs["key_password"]
                if "key_password" in self._kwargs
                else None,
                ca_cert_data=self._kwargs["ca_cert_data"]
                if "ca_cert_data" in self._kwargs
                else None,
                certdata=self._kwargs["cert_data"]
                if "cert_data" in self._kwargs
                else None,
                keydata=self._kwargs["key_data"]
                if "key_data" in self._kwargs
                else None,
            )
            self._connect_finalized.set()

        return await super().getaddrinfo(
            host,
            port,
            family=family,
            type=type,
            proto=proto,
            flags=flags,
            quic_upgrade_via_dns_rr=quic_upgrade_via_dns_rr,
        )


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
