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

pytest.importorskip("wsproto")

from wsproto import ConnectionType, WSConnection  # noqa: E402
from wsproto.events import AcceptConnection, BytesMessage, TextMessage  # noqa: E402

from urllib3 import AsyncPoolManager, AsyncProxyManager, PoolManager, ProxyManager  # noqa: E402
from urllib3.contrib.ssa import AsyncSocket  # noqa: E402
from urllib3.exceptions import ReadTimeoutError  # noqa: E402
from urllib3.util.wait import wait_for_read  # noqa: E402


# Cancelling an executor future cannot stop a stranded synchronous reader.
pytestmark = pytest.mark.timeout(60, method="thread")


class Peer:
    def __init__(self: Any, initial_message: str | None = None) -> None:
        self.protocol = WSConnection(ConnectionType.SERVER)
        self.writer: asyncio.StreamWriter | None = None
        self.received: asyncio.Queue[str | bytes] = asyncio.Queue()
        self.finished = asyncio.Event()
        self.initial_message = initial_message

    async def handle(
        self: Any, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.writer = writer
        try:
            self.protocol.receive_data(await reader.readuntil(b"\r\n\r\n"))
            list(self.protocol.events())
            handshake = self.protocol.send(AcceptConnection())
            if self.initial_message is not None:
                handshake += self.protocol.send(TextMessage(self.initial_message))
            writer.write(handshake)
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                self.protocol.receive_data(data)
                for event in self.protocol.events():
                    if isinstance(event, (TextMessage, BytesMessage)):
                        self.received.put_nowait(event.data)
        except ConnectionResetError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionResetError:
                pass
            self.finished.set()

    def send(self: Any, *messages: TextMessage | BytesMessage) -> None:
        assert self.writer is not None
        self.writer.write(b"".join(self.protocol.send(m) for m in messages))


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
            peer.send(TextMessage("first", message_finished=False))
            await asyncio.wait_for(waiting.get(), 5)
            await asyncio.wait_for(call(ws.send_payload, b"between fragments"), 2)
            assert (
                await asyncio.wait_for(peer.received.get(), 2) == b"between fragments"
            )
            peer.send(TextMessage("last"))
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
            peer.send(TextMessage("one"), BytesMessage(b"two"))
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
        peer.send(TextMessage("after timeout"))
        assert await asyncio.wait_for(call(ws.next_payload), 2) == "after timeout"


@pytest.mark.asyncio
async def test_buffered_transport_input(
    connection: Any, asynchronous: bool, tls: bool
) -> None:
    async with connection(asynchronous, tls) as (ws, peer, call, response):
        # One TLS record / asyncio buffer contains far more than one backend read.
        response._fp.from_promise._conn.blocksize = 64
        payload = b"x" * 8000
        peer.send(BytesMessage(payload))
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
        peer.send(TextMessage("first reader still owns the message"))
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
        peer.send(TextMessage("incomplete", message_finished=False))
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
            peer.send(TextMessage("still buffered"))
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
        peer.send(TextMessage("incoming"))
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
        peer.send(*[TextMessage("x" * 100, message_finished=False) for _ in range(100)])
        read = asyncio.ensure_future(call(ws.next_payload))
        await asyncio.wait_for(first_chunk.wait(), 2)
        await asyncio.wait_for(call(ws.send_payload, "writer"), 2)
        assert await asyncio.wait_for(peer.received.get(), 2) == "writer"
        assert not read.done()
        assert written_at[0] < 150
        peer.send(TextMessage("end"))
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
        peer.send(TextMessage("valid"))
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
