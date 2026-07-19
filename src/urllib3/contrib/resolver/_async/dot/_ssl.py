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
