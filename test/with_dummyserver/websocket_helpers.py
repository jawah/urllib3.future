from __future__ import annotations

import asyncio
import functools
import ssl
from contextlib import asynccontextmanager
from threading import Event
from typing import Any, AsyncGenerator, Mapping

import pytest
import trustme

from urllib3 import AsyncPoolManager, AsyncProxyManager, PoolManager, ProxyManager
from urllib3.contrib.ssa import AsyncSocket

try:
    import wsproto
except ImportError:
    wsproto = None  # type: ignore[assignment]
    pytest.importorskip("websockets")
    from websockets.frames import Frame, Opcode
    from websockets.server import ServerProtocol


class Peer:
    def __init__(
        self: Any,
        initial_message: str | None = None,
        *,
        compression: bool = False,
        handshake_error: str | None = None,
    ) -> None:
        self.compression = compression
        self.handshake_error = handshake_error
        self.protocol = (
            wsproto.WSConnection(wsproto.ConnectionType.SERVER)
            if wsproto is not None
            else ServerProtocol()
        )
        if wsproto is None and compression:
            from websockets.extensions.permessage_deflate import (
                ServerPerMessageDeflateFactory,
            )

            self.protocol = ServerProtocol(
                extensions=[ServerPerMessageDeflateFactory()]
            )
        self.writer: asyncio.StreamWriter | None = None
        self.received: asyncio.Queue[str | bytes] = asyncio.Queue()
        self.pongs: asyncio.Queue[bytes] = asyncio.Queue()
        self.finished = asyncio.Event()
        self.initial_message = initial_message
        self.request_headers = b""

    async def handle(
        self: Any, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.writer = writer
        try:
            self.request_headers = await reader.readuntil(b"\r\n\r\n")
            self.protocol.receive_data(self.request_headers)
            if wsproto is not None:
                list(self.protocol.events())
                handshake = self.protocol.send(
                    wsproto.events.AcceptConnection(
                        extensions=[wsproto.extensions.PerMessageDeflate()]
                        if self.compression
                        else [],
                    )
                )
            else:
                request = self.protocol.events_received()[0]
                self.protocol.send_response(self.protocol.accept(request))
                handshake = b"".join(self.protocol.data_to_send())
            if self.handshake_error == "missing-accept":
                handshake = b"\r\n".join(
                    line
                    for line in handshake.split(b"\r\n")
                    if not line.lower().startswith(b"sec-websocket-accept:")
                )
            elif self.handshake_error == "wrong-accept":
                handshake = b"\r\n".join(
                    b"Sec-WebSocket-Accept: invalid"
                    if line.lower().startswith(b"sec-websocket-accept:")
                    else line
                    for line in handshake.split(b"\r\n")
                )
            elif self.handshake_error == "unknown-extension":
                handshake = handshake.replace(
                    b"\r\n\r\n", b"\r\nSec-WebSocket-Extensions: unknown\r\n\r\n"
                )
            if self.initial_message is not None:
                handshake += self.encode(self.initial_message)
            writer.write(handshake)
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                self.protocol.receive_data(data)
                if wsproto is not None:
                    for event in self.protocol.events():
                        if isinstance(
                            event,
                            (wsproto.events.TextMessage, wsproto.events.BytesMessage),
                        ):
                            self.received.put_nowait(event.data)
                        elif isinstance(event, wsproto.events.Pong):
                            self.pongs.put_nowait(event.payload)
                else:
                    for event in self.protocol.events_received():
                        if isinstance(event, Frame):
                            if event.opcode is Opcode.TEXT:
                                self.received.put_nowait(event.data.decode())
                            elif event.opcode is Opcode.BINARY:
                                self.received.put_nowait(event.data)
                            elif event.opcode is Opcode.PONG:
                                self.pongs.put_nowait(bytes(event.data))
        except ConnectionResetError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionResetError:
                pass
            self.finished.set()

    def encode(self: Any, message: str | bytes, message_finished: bool = True) -> bytes:
        if wsproto is not None:
            event = (
                wsproto.events.TextMessage(message, message_finished=message_finished)
                if isinstance(message, str)
                else wsproto.events.BytesMessage(
                    message, message_finished=message_finished
                )
            )
            encoded: bytes = self.protocol.send(event)
            return encoded
        data = message.encode() if isinstance(message, str) else message
        if self.protocol.expect_continuation_frame:
            self.protocol.send_continuation(data, fin=message_finished)
        elif isinstance(message, str):
            self.protocol.send_text(data, fin=message_finished)
        else:
            self.protocol.send_binary(data, fin=message_finished)
        return b"".join(self.protocol.data_to_send())

    def send(self: Any, *messages: str | bytes, message_finished: bool = True) -> None:
        assert self.writer is not None
        self.writer.write(b"".join(self.encode(m, message_finished) for m in messages))


@asynccontextmanager
async def _connection(
    asynchronous: bool,
    tls: bool,
    timeout: float | None = None,
    initial_message: str | None = None,
    *,
    implementation: str,
    tls_proxy: bool = False,
    compression: bool = False,
    handshake_error: str | None = None,
    headers: Mapping[str, str] | None = None,
    blocksize: int | None = None,
) -> AsyncGenerator[tuple[Any, Peer, Any, Any], None]:
    socket_options: dict[str, Any] = (
        {} if blocksize is None else {"blocksize": blocksize}
    )
    peer = Peer(
        initial_message, compression=compression, handshake_error=handshake_error
    )
    server_context = None
    client_context = None
    if tls:
        ca = trustme.CA()
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ca.issue_cert("127.0.0.1").configure_cert(server_context)
        client_context = ssl.create_default_context()
        ca.configure_trust(client_context)
    server = await asyncio.start_server(peer.handle, "127.0.0.1", 0, ssl=server_context)
    port = server.sockets[0].getsockname()[1]
    manager: Any
    proxy = None
    tunnel_finished = asyncio.Event()
    if tls_proxy:
        assert tls

        async def tunnel(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            upstream_writer = None
            pumps = []
            try:
                request = await reader.readuntil(b"\r\n\r\n")
                assert request.startswith(b"CONNECT ")
                upstream_reader, upstream_writer = await asyncio.open_connection(
                    "127.0.0.1", port
                )
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")

                async def copy(
                    source: asyncio.StreamReader, target: asyncio.StreamWriter
                ) -> None:
                    while True:
                        data = await source.read(65536)
                        if not data:
                            break
                        target.write(data)
                        await target.drain()

                pumps = [
                    asyncio.create_task(copy(reader, upstream_writer)),
                    asyncio.create_task(copy(upstream_reader, writer)),
                ]
                await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for pump in pumps:
                    pump.cancel()
                await asyncio.gather(*pumps, return_exceptions=True)
                if upstream_writer is not None:
                    upstream_writer.close()
                writer.close()
                tunnel_finished.set()

        proxy = await asyncio.start_server(tunnel, "127.0.0.1", 0, ssl=server_context)
        proxy_port = proxy.sockets[0].getsockname()[1]
        manager = (AsyncProxyManager if asynchronous else ProxyManager)(
            f"https://127.0.0.1:{proxy_port}",
            timeout=timeout,
            ssl_context=client_context,
            proxy_ssl_context=client_context,
            **socket_options,
        )
    else:
        manager = (AsyncPoolManager if asynchronous else PoolManager)(
            timeout=timeout,
            ssl_context=client_context,
            **socket_options,
        )

    async def call(fn: Any, *args: Any, **kwargs: Any) -> Any:
        if asynchronous:
            return await fn(*args, **kwargs)
        return await asyncio.get_running_loop().run_in_executor(
            None, functools.partial(fn, *args, **kwargs)
        )

    response = None
    try:
        response = await call(
            manager.urlopen,
            "GET",
            f"{'wss' if tls else 'ws'}+{implementation}://127.0.0.1:{port}/",
            timeout=5,
            retries=False,
            headers=headers,
        )
        assert response.version == 11
        # Short read deadlines belong to the WebSocket operation, not its handshake.
        conn = response._fp.from_promise._conn
        conn.timeout = timeout
        conn.sock.settimeout(timeout)
        yield response.extension, peer, call, response
    finally:
        if response is not None:
            await call(response.extension.close)
        await call(manager.clear)
        if peer.writer is not None:
            # Let the peer consume the final close frame before TLS shutdown.
            await asyncio.wait_for(peer.finished.wait(), 5)
        server.close()
        await server.wait_closed()
        if proxy is not None:
            proxy.close()
            await proxy.wait_closed()
            await asyncio.wait_for(tunnel_finished.wait(), 5)


def observe_wait(
    monkeypatch: pytest.MonkeyPatch, asynchronous: bool, ws: Any
) -> asyncio.Queue[None]:
    """Observe entry into the actual readiness wait without timing sleeps."""
    loop = asyncio.get_running_loop()
    waiting: asyncio.Queue[None] = asyncio.Queue()
    if asynchronous:
        original = AsyncSocket.until_data_available

        async def wait(sock: AsyncSocket, remaining: float | None = None) -> None:
            waiting.put_nowait(None)
            await original(sock, remaining)

        monkeypatch.setattr(AsyncSocket, "until_data_available", wait)
    else:
        cls = type(ws)
        original = cls._wait_for_read

        def sync_wait(ext: Any, sock: Any) -> Any:
            loop.call_soon_threadsafe(waiting.put_nowait, None)
            return original(ext, sock)

        monkeypatch.setattr(cls, "_wait_for_read", sync_wait)
    return waiting


@asynccontextmanager
async def held_websocket_connection(
    ws: Any, response: Any, call: Any, asynchronous: bool
) -> AsyncGenerator[None, None]:
    acquired = asyncio.Event()
    release: Any = asyncio.Event() if asynchronous else Event()
    loop = asyncio.get_running_loop()

    async def async_owner() -> None:
        async with ws._police_officer.borrow(response):
            acquired.set()
            await release.wait()

    def sync_owner() -> None:
        with ws._police_officer.borrow(response):
            loop.call_soon_threadsafe(acquired.set)
            assert release.wait(10)

    owner = asyncio.ensure_future(call(async_owner if asynchronous else sync_owner))
    try:
        await asyncio.wait_for(acquired.wait(), 5)
        yield
    finally:
        release.set()
        await asyncio.wait_for(owner, 5)


def observe_websocket_borrows(
    ws: Any, asynchronous: bool, monkeypatch: pytest.MonkeyPatch
) -> asyncio.Queue[None]:
    queued: asyncio.Queue[None] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    target = ws._police_officer._signals if asynchronous else ws._police_officer
    method = "register" if asynchronous else "_register_signal"
    original = getattr(target, method)

    def register(*args: Any) -> Any:
        signal = original(*args)
        loop.call_soon_threadsafe(queued.put_nowait, None)
        return signal

    monkeypatch.setattr(target, method, register)
    return queued
