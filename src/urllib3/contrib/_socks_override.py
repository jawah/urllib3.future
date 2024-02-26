"""
This is hazmat. It can blow up anytime.
Use it with precautions!

Reasoning behind this:

1) python-socks requires another dependency, namely asyncio-timeout, that is one too much for us.
2) it does not support our AsyncSocket wrapper (it has his own internally)
"""
from __future__ import annotations

import asyncio
import socket
import typing

from python_socks import _abc as abc  # type: ignore

# look the other way if unpleasant. No choice for now.
# will start discussions once we have a solid traffic.
from python_socks._connectors.abc import AsyncConnector  # type: ignore
from python_socks._connectors.socks4_async import Socks4AsyncConnector  # type: ignore
from python_socks._connectors.socks5_async import Socks5AsyncConnector  # type: ignore
from python_socks._errors import ProxyError, ProxyTimeoutError  # type: ignore
from python_socks._helpers import parse_proxy_url  # type: ignore
from python_socks._protocols.errors import ReplyError  # type: ignore
from python_socks._types import ProxyType  # type: ignore

from .ssa import AsyncSocket


class Resolver(abc.AsyncResolver):  # type: ignore[misc]
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_UNSPEC
    ) -> tuple[socket.AddressFamily, str]:
        infos = await self._loop.getaddrinfo(
            host=host,
            port=port,
            family=family,
            type=socket.SOCK_STREAM,
        )

        if not infos:  # Defensive:
            raise OSError(f"Can`t resolve address {host}:{port} [{family}]")

        infos = sorted(infos, key=lambda info: info[0])

        family, _, _, _, address = infos[0]
        return family, address[0]


def create_connector(
    proxy_type: ProxyType,
    username: str | None,
    password: str | None,
    rdns: bool,
    resolver: abc.AsyncResolver,
) -> AsyncConnector:
    if proxy_type == ProxyType.SOCKS4:
        return Socks4AsyncConnector(
            user_id=username,
            rdns=rdns,
            resolver=resolver,
        )

    if proxy_type == ProxyType.SOCKS5:
        return Socks5AsyncConnector(
            username=username,
            password=password,
            rdns=rdns,
            resolver=resolver,
        )

    raise ValueError(f"Invalid proxy type: {proxy_type}")


class AsyncioProxy(abc.AsyncProxy):  # type: ignore[misc]
    def __init__(
        self,
        proxy_type: ProxyType,
        host: str,
        port: int,
        username: str | None = None,
        password: str | None = None,
        rdns: bool = False,
    ):
        self._loop = asyncio.get_event_loop()

        self._proxy_type = proxy_type
        self._proxy_host = host
        self._proxy_port = port
        self._password = password
        self._username = username
        self._rdns = rdns

        self._resolver = Resolver(loop=self._loop)

    async def connect(
        self,
        dest_host: str,
        dest_port: int,
        timeout: float | None = None,
        _socket: AsyncSocket | None = None,
    ) -> AsyncSocket:
        if timeout is None:
            timeout = 60

        try:
            return await asyncio.wait_for(
                self._connect(
                    dest_host=dest_host,
                    dest_port=dest_port,
                    _socket=_socket,  # type: ignore[arg-type]
                ),
                timeout,
            )
        except asyncio.TimeoutError as e:
            raise ProxyTimeoutError(f"Proxy connection timed out: {timeout}") from e

    async def _connect(
        self, dest_host: str, dest_port: int, _socket: AsyncSocket
    ) -> AsyncSocket:
        try:
            connector = create_connector(
                proxy_type=self._proxy_type,
                username=self._username,
                password=self._password,
                rdns=self._rdns,
                resolver=self._resolver,
            )
            await connector.connect(
                stream=_socket,
                host=dest_host,
                port=dest_port,
            )

            return _socket
        except asyncio.CancelledError:  # Defensive:
            _socket.close()
            raise
        except ReplyError as e:
            _socket.close()
            raise ProxyError(e, error_code=e.error_code)
        except Exception:  # Defensive:
            _socket.close()
            raise

    @property
    def proxy_host(self) -> str:
        return self._proxy_host

    @property
    def proxy_port(self) -> int:
        return self._proxy_port

    @classmethod
    def create(cls, *args: typing.Any, **kwargs: typing.Any) -> AsyncioProxy:
        return cls(*args, **kwargs)

    @classmethod
    def from_url(cls, url: str, **kwargs: typing.Any) -> AsyncioProxy:
        url_args = parse_proxy_url(url)
        return cls(*url_args, **kwargs)
