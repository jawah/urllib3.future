from __future__ import annotations

import asyncio
import functools
import ssl
import sys
from contextlib import asynccontextmanager
from threading import Event, Lock
from typing import Any, AsyncGenerator

import pytest
import trustme

from urllib3 import AsyncPoolManager, AsyncProxyManager, PoolManager, ProxyManager
from urllib3.contrib.ssa import AsyncSocket
from urllib3.exceptions import ReadTimeoutError, SSLError
from urllib3.util.wait import wait_for_read

try:
    import wsproto
except ImportError:
    wsproto = None  # type: ignore[assignment]
    pytest.importorskip("websockets")
    from websockets.frames import Frame, Opcode
    from websockets.server import ServerProtocol


# Cancelling an executor future cannot stop a stranded synchronous reader.
pytestmark = pytest.mark.timeout(60, method="thread")


class Peer:
    def __init__(self: Any, initial_message: str | None = None) -> None:
        self.protocol = (
            wsproto.WSConnection(wsproto.ConnectionType.SERVER)
            if wsproto is not None
            else ServerProtocol()
        )
        self.writer: asyncio.StreamWriter | None = None
        self.received: asyncio.Queue[str | bytes] = asyncio.Queue()
        self.pongs: asyncio.Queue[bytes] = asyncio.Queue()
        self.finished = asyncio.Event()
        self.initial_message = initial_message

    async def handle(
        self: Any, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.writer = writer
        try:
            self.protocol.receive_data(await reader.readuntil(b"\r\n\r\n"))
            if wsproto is not None:
                list(self.protocol.events())
                handshake = self.protocol.send(wsproto.events.AcceptConnection())
            else:
                request = self.protocol.events_received()[0]
                self.protocol.send_response(self.protocol.accept(request))
                handshake = b"".join(self.protocol.data_to_send())
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


@pytest.fixture(params=[False, True], ids=["sync", "async"])
def asynchronous(request: pytest.FixtureRequest) -> bool:
    return bool(request.param)


@pytest.fixture(params=[False, True], ids=["tcp", "tls"])
def tls(request: pytest.FixtureRequest) -> bool:
    return bool(request.param)


@asynccontextmanager
async def _connection(
    asynchronous: bool,
    tls: bool,
    timeout: float | None = None,
    initial_message: str | None = None,
    *,
    implementation: str,
    tls_proxy: bool = False,
) -> AsyncGenerator[tuple[Any, Peer, Any, Any], None]:
    peer = Peer(initial_message)
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
        )
    else:
        manager = (AsyncPoolManager if asynchronous else PoolManager)(
            timeout=timeout, ssl_context=client_context
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
        )
        assert response.version == 11
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


@pytest.fixture(params=["wsproto", "fast"])
def connection(request: pytest.FixtureRequest) -> Any:
    if request.param == "fast":
        pytest.importorskip("websockets", minversion="15.0")
    else:
        pytest.importorskip("wsproto")
    return functools.partial(_connection, implementation=request.param)


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


@pytest.mark.asyncio
async def test_write_during_read_and_fragmentation(
    connection: Any, asynchronous: bool, tls: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with connection(asynchronous, tls) as (ws, peer, call, _):
        waiting = observe_wait(monkeypatch, asynchronous, ws)
        read = asyncio.ensure_future(call(ws.next_payload))
        try:
            await asyncio.wait_for(waiting.get(), 5)
            await asyncio.wait_for(call(ws.send_payload, "outgoing"), 2)
            assert await asyncio.wait_for(peer.received.get(), 2) == "outgoing"
            assert not read.done()
            peer.send("first", message_finished=False)
            await asyncio.wait_for(waiting.get(), 5)
            await asyncio.wait_for(call(ws.send_payload, b"between fragments"), 2)
            assert (
                await asyncio.wait_for(peer.received.get(), 2) == b"between fragments"
            )
            peer.send("last")
            assert await asyncio.wait_for(read, 5) == "firstlast"
        finally:
            await call(ws.close)
            await asyncio.gather(read, return_exceptions=True)


@pytest.mark.asyncio
async def test_two_readers_with_coalesced_messages(
    connection: Any, asynchronous: bool, tls: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with connection(asynchronous, tls) as (ws, peer, call, _):
        waiting = observe_wait(monkeypatch, asynchronous, ws)
        first = asyncio.ensure_future(call(ws.next_payload))
        await asyncio.wait_for(waiting.get(), 5)
        second = asyncio.ensure_future(call(ws.next_payload))
        try:
            peer.send("one", b"two")
            assert await asyncio.wait_for(first, 5) == "one"
            assert await asyncio.wait_for(second, 5) == b"two"
        finally:
            await call(ws.close)
            await asyncio.gather(first, second, return_exceptions=True)


@pytest.mark.asyncio
async def test_close_wakes_indefinite_read(
    connection: Any, asynchronous: bool, tls: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with connection(asynchronous, tls) as (ws, _, call, _):
        waiting = observe_wait(monkeypatch, asynchronous, ws)
        read = asyncio.ensure_future(call(ws.next_payload))
        await asyncio.wait_for(waiting.get(), 5)
        await asyncio.wait_for(call(ws.close), 2)
        assert await asyncio.wait_for(read, 2) is None


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform == "win32", reason="Read-side shutdown does not wake Windows select()"
)
@pytest.mark.parametrize("tls_proxy", [False, True], ids=["tcp", "tls-proxy"])
async def test_close_waits_for_readiness_wait(
    connection: Any, tls_proxy: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with connection(False, tls_proxy, tls_proxy=tls_proxy) as (ws, _, call, _):
        # Exercise the macOS/DragonFly close ordering on other POSIX platforms.
        ws._read_wait_lock = Lock()
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        woken = asyncio.Event()
        resume = Event()
        sockets = []

        def wait(sock: Any, timeout: float | None = None) -> bool:
            sockets.append(sock)
            loop.call_soon_threadsafe(entered.set)
            result = wait_for_read(sock, timeout)
            loop.call_soon_threadsafe(woken.set)
            assert resume.wait(5)
            return result

        monkeypatch.setattr(f"{type(ws).__module__}.wait_for_read", wait)
        read = asyncio.ensure_future(call(ws.next_payload))
        close = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            close = asyncio.ensure_future(call(ws.close))
            await asyncio.wait_for(woken.wait(), 5)
            # The reader hasn't finished handling the shutdown notification.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(close), 0.05)
            assert sockets[0].fileno() >= 0
            resume.set()
            await asyncio.wait_for(close, 2)
            assert await asyncio.wait_for(read, 2) is None
        finally:
            resume.set()
            if close is not None:
                await asyncio.gather(close, return_exceptions=True)
            await call(ws.close)
            await asyncio.gather(read, return_exceptions=True)


@pytest.mark.asyncio
async def test_timeout_preserves_extension(
    connection: Any, asynchronous: bool, tls: bool
) -> None:
    async with connection(asynchronous, tls, timeout=0.2) as (ws, peer, call, _):
        with pytest.raises(ReadTimeoutError):
            await asyncio.wait_for(call(ws.next_payload), 2)
        assert not ws.closed
        peer.send("after timeout")
        assert await asyncio.wait_for(call(ws.next_payload), 2) == "after timeout"


@pytest.mark.asyncio
async def test_buffered_transport_input(
    connection: Any, asynchronous: bool, tls: bool
) -> None:
    async with connection(asynchronous, tls) as (ws, peer, call, response):
        # One TLS record / asyncio buffer contains far more than one backend read.
        response._fp.from_promise._conn.blocksize = 64
        payload = b"x" * 8000
        peer.send(payload)
        assert await asyncio.wait_for(call(ws.next_payload), 5) == payload


@pytest.mark.asyncio
async def test_cancel_waiting_reader(
    connection: Any, tls: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with connection(True, tls) as (ws, _, call, response):
        waiting = observe_wait(monkeypatch, True, ws)
        read = asyncio.ensure_future(call(ws.next_payload))
        await asyncio.wait_for(waiting.get(), 5)
        reader = response._fp.from_promise._conn.sock._reader
        read.cancel()
        with pytest.raises(asyncio.CancelledError):
            await read
        assert reader._waiter is None
        assert ws.closed


@pytest.mark.asyncio
async def test_message_buffered_with_handshake(
    connection: Any, asynchronous: bool, tls: bool
) -> None:
    async with connection(asynchronous, tls, initial_message="already here") as (
        ws,
        _,
        call,
        _,
    ):
        assert await asyncio.wait_for(call(ws.next_payload), 2) == "already here"


@pytest.mark.asyncio
@pytest.mark.parametrize("buffered", [False, True])
async def test_control_frames_between_messages(
    connection: Any, asynchronous: bool, tls: bool, buffered: bool
) -> None:
    async with connection(asynchronous, tls) as (ws, peer, call, _):
        assert peer.writer is not None
        data = peer.encode("first") if buffered else b""
        # A server ping requires a matching pong; an unsolicited pong is ignored.
        data += b"\x89\x03abc\x8a\x04pong" + peer.encode("after control frames")
        peer.writer.write(data)
        if buffered:
            assert await asyncio.wait_for(call(ws.next_payload), 2) == "first"
        assert (
            await asyncio.wait_for(call(ws.next_payload), 2) == "after control frames"
        )
        assert await asyncio.wait_for(peer.pongs.get(), 2) == b"abc"
        assert not ws.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frames, expected",
    [
        (b"\x02\x03one\x80\x03two", b"onetwo"),
        # The UTF-8 character crosses a WebSocket frame boundary.
        (b"\x01\x02\xf0\x9f\x80\x02\x9a\x80", "\U0001f680"),
    ],
)
async def test_fragmented_message_followed_by_another_message(
    connection: Any, asynchronous: bool, tls: bool, frames: bytes, expected: str | bytes
) -> None:
    async with connection(asynchronous, tls) as (ws, peer, call, _):
        assert peer.writer is not None
        peer.writer.write(frames + peer.encode("next message"))
        assert await asyncio.wait_for(call(ws.next_payload), 2) == expected
        assert await asyncio.wait_for(call(ws.next_payload), 2) == "next message"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame",
    [
        pytest.param(b"\x88\x02\x03\xe8", id="normal-close"),
        pytest.param(b"\x81\x01\xff", id="invalid-utf8"),
        pytest.param(b"\xc1\x01x", id="unnegotiated-rsv1"),
        pytest.param(b"\x81\x81\x00\x00\x00\x00x", id="masked-server-frame"),
    ],
)
async def test_peer_close_or_invalid_frame_releases_connection(
    connection: Any, asynchronous: bool, tls: bool, frame: bytes
) -> None:
    async with connection(asynchronous, tls) as (ws, peer, call, _):
        assert peer.writer is not None
        peer.writer.write(frame)
        assert await asyncio.wait_for(call(ws.next_payload), 2) is None
        assert ws.closed
        for operation, args in (
            (ws.next_payload, ()),
            (ws.send_payload, ("too late",)),
            (ws.ping, ()),
        ):
            with pytest.raises(OSError, match="closed or uninitialized"):
                await call(operation, *args)


@pytest.mark.asyncio
async def test_peer_disconnect_releases_connection(
    connection: Any, asynchronous: bool, tls: bool
) -> None:
    async with connection(asynchronous, tls) as (ws, peer, call, _):
        assert peer.writer is not None
        # Close the transport without a WebSocket closing handshake.
        peer.writer.transport.abort()
        await asyncio.wait_for(peer.finished.wait(), 2)
        try:
            assert await asyncio.wait_for(call(ws.next_payload), 2) is None
        except SSLError as exc:
            # Older OpenSSL/Python combinations report missing close_notify as an error.
            assert tls
            assert "unexpected eof" in str(exc).lower()
        assert ws.closed


@pytest.mark.asyncio
async def test_cancel_queued_reader(
    connection: Any, tls: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with connection(True, tls) as (ws, peer, call, _):
        waiting = observe_wait(monkeypatch, True, ws)
        first = asyncio.ensure_future(call(ws.next_payload))
        await asyncio.wait_for(waiting.get(), 5)
        second = asyncio.ensure_future(call(ws.next_payload))
        # Give the second task its turn to reach the reader lock.
        await asyncio.sleep(0)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        assert not ws.closed
        peer.send("first reader still owns the message")
        assert await asyncio.wait_for(first, 2) == "first reader still owns the message"


@pytest.mark.asyncio
async def test_cancel_between_borrows(
    connection: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import asynccontextmanager

    async with connection(True, False) as (ws, peer, call, _):
        borrow = ws._police_officer.borrow
        receive_data = ws._protocol.receive_data
        received_fragment = False
        writers = []

        def receive(data: bytes) -> None:
            nonlocal received_fragment
            receive_data(data)
            received_fragment = True
            writers.append(asyncio.create_task(ws.send_payload(b"force handoff")))

        @asynccontextmanager
        async def cancel_after_fragment(*args: Any, **kwargs: Any) -> Any:
            nonlocal received_fragment
            async with borrow(*args, **kwargs) as conn:
                yield conn
            if received_fragment and asyncio.current_task() is read:
                received_fragment = False
                task = asyncio.current_task()
                assert task is not None
                task.cancel()
                await asyncio.sleep(0)

        monkeypatch.setattr(ws._protocol, "receive_data", receive)
        monkeypatch.setattr(ws._police_officer, "borrow", cancel_after_fragment)
        peer.send("incomplete", message_finished=False)
        read = asyncio.ensure_future(call(ws.next_payload))
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(read, 2)
        assert ws.closed
        await asyncio.gather(*writers, return_exceptions=True)


@pytest.mark.asyncio
async def test_read_deadline_includes_reacquiring_connection(
    connection: Any, asynchronous: bool, tls: bool
) -> None:
    from threading import Event

    async with connection(asynchronous, tls, timeout=0.2) as (ws, peer, call, response):
        import pytest

        monkeypatch = pytest.MonkeyPatch()
        waiting = observe_wait(monkeypatch, asynchronous, ws)
        read = asyncio.ensure_future(call(ws.next_payload))
        await asyncio.wait_for(waiting.get(), 2)
        holding = asyncio.Event()
        release: Any = asyncio.Event() if asynchronous else Event()
        loop = asyncio.get_running_loop()
        police = ws._police_officer
        if asynchronous:

            async def async_hold() -> None:
                async with police.borrow(response):
                    holding.set()
                    await release.wait()
        else:

            def sync_hold() -> Any:
                with police.borrow(response):
                    loop.call_soon_threadsafe(holding.set)
                    assert release.wait(3)

        hold = async_hold if asynchronous else sync_hold

        writer = asyncio.ensure_future(call(hold))
        try:
            await asyncio.wait_for(holding.wait(), 2)
            peer.send("still buffered")
            with pytest.raises(ReadTimeoutError):
                await asyncio.wait_for(read, 1)
            assert not ws.closed
        finally:
            release.set()
            await asyncio.wait_for(writer, 2)
            monkeypatch.undo()
        assert await asyncio.wait_for(call(ws.next_payload), 2) == "still buffered"


@pytest.mark.asyncio
async def test_close_after_readiness_before_reacquire(
    connection: Any, asynchronous: bool, tls: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Event

    async with connection(asynchronous, tls) as (ws, peer, call, response):
        ready = asyncio.Event()
        entered = asyncio.Event()
        release: Any = asyncio.Event() if asynchronous else Event()
        loop = asyncio.get_running_loop()
        original = ws._wait_for_read
        if asynchronous:

            async def async_wait(sock: Any) -> None:
                entered.set()
                await original(sock)
                ready.set()
                await release.wait()
        else:

            def sync_wait(sock: Any) -> Any:
                loop.call_soon_threadsafe(entered.set)
                original(sock)
                loop.call_soon_threadsafe(ready.set)
                assert release.wait(3)

        wait = async_wait if asynchronous else sync_wait

        monkeypatch.setattr(ws, "_wait_for_read", wait)
        read = asyncio.ensure_future(call(ws.next_payload))
        # Data may arrive before the reader enters its wait: observe the wait
        # itself so this tests exactly the readiness/reacquisition race.
        await asyncio.wait_for(entered.wait(), 2)
        peer.send("incoming")
        await asyncio.wait_for(ready.wait(), 2)
        try:
            await asyncio.wait_for(call(ws.close), 2)
        finally:
            release.set()
        assert await asyncio.wait_for(read, 2) is None


@pytest.mark.asyncio
async def test_buffered_fragments_allow_writer_progress(
    connection: Any, asynchronous: bool, tls: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Event

    async with connection(asynchronous, tls) as (ws, peer, call, response):
        first_chunk = asyncio.Event()
        written_at = []
        original_send = ws._dsa.sendall
        original_receive = ws._protocol.receive_data
        loop = asyncio.get_running_loop()
        seen = 0
        queued = Event()
        if not asynchronous:
            register = ws._police_officer._register_signal

            def registered(*args: Any, **kwargs: Any) -> Any:
                result = register(*args, **kwargs)
                queued.set()
                return result

            monkeypatch.setattr(ws._police_officer, "_register_signal", registered)

        def receive(data: bytes) -> Any:
            nonlocal seen
            original_receive(data)
            seen += 1
            if seen == 1:
                loop.call_soon_threadsafe(first_chunk.set)
                if not asynchronous:
                    assert queued.wait(2)

        monkeypatch.setattr(ws._protocol, "receive_data", receive)
        if asynchronous:

            async def async_send(data: bytes) -> None:
                written_at.append(seen)
                await original_send(data)
        else:

            def sync_send(data: bytes) -> Any:
                written_at.append(seen)
                original_send(data)

        send = async_send if asynchronous else sync_send

        monkeypatch.setattr(ws._dsa, "sendall", send)
        response._fp.from_promise._conn.blocksize = 64
        # Buffer many incomplete fragments. The read must give the writer a
        # turn before another socket-readiness suspension is needed.
        peer.send(*["x" * 100 for _ in range(100)], message_finished=False)
        read = asyncio.ensure_future(call(ws.next_payload))
        await asyncio.wait_for(first_chunk.wait(), 2)
        await asyncio.wait_for(call(ws.send_payload, "writer"), 2)
        assert await asyncio.wait_for(peer.received.get(), 2) == "writer"
        assert not read.done()
        assert written_at[0] < 150
        peer.send("end")
        assert await asyncio.wait_for(read, 3) == "x" * 10000 + "end"


@pytest.mark.asyncio
async def test_close_before_wait_enters_socket(
    connection: Any, asynchronous: bool, tls: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Event

    async with connection(asynchronous, tls) as (ws, peer, call, response):
        entered = asyncio.Event()
        released: Any = asyncio.Event() if asynchronous else Event()
        original = ws._wait_for_read
        loop = asyncio.get_running_loop()
        if asynchronous:

            async def async_wait(sock: Any) -> None:
                entered.set()
                await released.wait()
                await original(sock)
        else:

            def sync_wait(sock: Any) -> Any:
                loop.call_soon_threadsafe(entered.set)
                assert released.wait(2)
                original(sock)

        wait = async_wait if asynchronous else sync_wait

        monkeypatch.setattr(ws, "_wait_for_read", wait)
        read = asyncio.ensure_future(call(ws.next_payload))
        await asyncio.wait_for(entered.wait(), 2)
        try:
            await asyncio.wait_for(call(ws.close), 2)
        finally:
            released.set()
        assert await asyncio.wait_for(read, 2) is None


@pytest.mark.asyncio
async def test_reentrant_read_keeps_blocking_semantics(
    connection: Any, asynchronous: bool, tls: bool
) -> None:
    async with connection(asynchronous, tls, timeout=0.1) as (ws, peer, call, response):
        police = ws._police_officer
        if asynchronous:

            async def async_read() -> Any:
                async with police.borrow(response):
                    return await ws.next_payload()
        else:

            def sync_read() -> Any:
                with police.borrow(response):
                    return ws.next_payload()

        read = async_read if asynchronous else sync_read

        with pytest.raises(ReadTimeoutError):
            await asyncio.wait_for(call(read), 1)
        assert not ws.closed
        peer.send("valid")
        assert await asyncio.wait_for(call(read), 1) == "valid"


@pytest.mark.asyncio
async def test_concurrent_close_is_idempotent(
    connection: Any, asynchronous: bool, tls: bool
) -> None:
    async with connection(asynchronous, tls, timeout=1) as (ws, peer, call, response):
        police = ws._police_officer
        conn = response._fp.from_promise._conn
        await asyncio.wait_for(asyncio.gather(*[call(ws.close) for _ in range(8)]), 2)
        assert ws.closed
        assert id(conn) not in police._registry
        await asyncio.wait_for(peer.finished.wait(), 2)


@pytest.mark.asyncio
async def test_tls_proxy_duplex_and_close(
    connection: Any, asynchronous: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with connection(asynchronous, True, tls_proxy=True) as (ws, peer, call, _):
        waiting = observe_wait(monkeypatch, asynchronous, ws)
        read = asyncio.ensure_future(call(ws.next_payload))
        try:
            await asyncio.wait_for(waiting.get(), 5)
            await asyncio.wait_for(call(ws.send_payload, "through tunnel"), 2)
            assert await asyncio.wait_for(peer.received.get(), 2) == "through tunnel"
            await asyncio.wait_for(call(ws.close), 2)
            assert await asyncio.wait_for(read, 2) is None
        finally:
            await call(ws.close)
            await asyncio.gather(read, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "write"])
@pytest.mark.parametrize(
    "failure", ["timeout", "ssl-timeout", "ssl", "socket", "other"]
)
async def test_transport_error_translation_and_cleanup(
    connection: Any,
    asynchronous: bool,
    operation: str,
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from socket import timeout as SocketTimeout
    from urllib3.exceptions import ProtocolError

    errors = {
        "timeout": SocketTimeout("timed out"),
        "ssl-timeout": ssl.SSLError("read operation timed out"),
        "ssl": ssl.SSLError("bad record mac"),
        "socket": ConnectionResetError("peer reset"),
        "other": ValueError("unexpected transport failure"),
    }
    error = errors[failure]
    expected = {
        "timeout": ReadTimeoutError,
        "ssl-timeout": ReadTimeoutError if operation == "read" else SSLError,
        "ssl": SSLError,
        "socket": ProtocolError,
        "other": ValueError,
    }[failure]
    async with connection(asynchronous, False, timeout=5) as (ws, peer, call, response):
        dsa = ws._dsa
        method = "recv_extended" if operation == "read" else "sendall"
        original = getattr(dsa, method)

        def fail_once(*args: Any, **kwargs: Any) -> Any:
            # Restore before raising so the real close path can still send/close.
            monkeypatch.setattr(dsa, method, original)
            raise error

        monkeypatch.setattr(dsa, method, fail_once)
        if operation == "read":
            peer.send("still readable")
        with pytest.raises(expected) as caught:
            if operation == "read":
                await call(ws.next_payload)
            else:
                await call(ws.send_payload, "outgoing")
        if expected is not ValueError:
            assert caught.value.__cause__ is error
        if operation == "read" and failure in {"timeout", "ssl-timeout"}:
            assert not ws.closed
            assert await call(ws.next_payload) == "still readable"
        else:
            assert ws.closed
            assert response._police_officer is None or not response._police_officer.busy


@pytest.mark.asyncio
async def test_response_close_closes_extension_and_rejects_second_start(
    connection: Any,
    asynchronous: bool,
) -> None:
    async with connection(asynchronous, False, timeout=5) as (ws, _, call, response):
        with pytest.raises(OSError, match="already plugged in"):
            await call(response.start_extension, type(ws)())
        await call(response.close)
        assert ws.closed


@pytest.mark.asyncio
async def test_message_and_close_in_one_transport_read(
    connection: Any, asynchronous: bool
) -> None:
    async with connection(asynchronous, False, timeout=5) as (ws, peer, call, _):
        assert peer.writer is not None
        peer.writer.write(peer.encode("last message") + b"\x88\x02\x03\xe8")
        assert await call(ws.next_payload) == "last message"
        assert await call(ws.next_payload) is None
        assert ws.closed
