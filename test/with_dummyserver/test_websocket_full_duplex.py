from __future__ import annotations

import asyncio
import functools
import ssl
import sys
from threading import Event, Lock
from typing import Any, Iterator, Mapping

import pytest

from test.with_dummyserver.conftest import HTTP3Peer
from test.with_dummyserver.websocket_helpers import (
    held_websocket_connection,
    observe_wait,
    observe_websocket_borrows,
)
from urllib3.exceptions import ProtocolError, ReadTimeoutError, SSLError
from urllib3.util.wait import wait_for_read

# Cancelling an executor future cannot stop a stranded synchronous reader.
pytestmark = pytest.mark.timeout(60, method="thread")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tls,tls_proxy",
    [(False, False), (True, False), (True, True)],
    ids=["tcp", "tls", "tls-proxy"],
)
async def test_handshake_accepts_header_mapping_without_copy(
    websocket_connection: Any, tls: bool, tls_proxy: bool
) -> None:
    class Headers(Mapping[str, str]):
        def __init__(self) -> None:
            self.data = {"X-Caller": "preserved", "Accept": "application/custom"}

        def __getitem__(self, key: str) -> str:
            return self.data[key]

        def __iter__(self) -> Iterator[str]:
            return iter(self.data)

        def __len__(self) -> int:
            return len(self.data)

    headers = Headers()
    original = dict(headers)
    async with websocket_connection(tls, headers=headers, tls_proxy=tls_proxy) as (
        ws,
        peer,
        call,
        _,
    ):
        received = {
            key.lower(): value.strip()
            for line in peer.request_headers.split(b"\r\n")[1:]
            if line
            for key, value in (line.split(b":", 1),)
        }
        assert received[b"x-caller"] == b"preserved"
        assert received[b"accept"] == b"application/custom"
        assert received[b"upgrade"].lower() == b"websocket"
        assert received[b"sec-websocket-version"] == b"13"
        assert dict(headers) == original
        await call(ws.send_payload, "hello")
        assert await asyncio.wait_for(peer.received.get(), 5) == "hello"


@pytest.mark.asyncio
async def test_write_during_read_and_fragmentation(
    websocket_connection: Any, tls: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with websocket_connection(tls) as (ws, peer, call, _):
        waiting = observe_wait(monkeypatch, False, ws)
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
    websocket_connection: Any, tls: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with websocket_connection(tls) as (ws, peer, call, _):
        waiting = observe_wait(monkeypatch, False, ws)
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
    websocket_connection: Any, tls: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with websocket_connection(tls) as (ws, _, call, _):
        waiting = observe_wait(monkeypatch, False, ws)
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
    websocket_connection: Any, tls_proxy: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with websocket_connection(tls_proxy, tls_proxy=tls_proxy) as (ws, _, call, _):
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
    websocket_connection: Any, tls: bool
) -> None:
    async with websocket_connection(tls, timeout=0.2) as (ws, peer, call, _):
        with pytest.raises(ReadTimeoutError):
            await asyncio.wait_for(call(ws.next_payload), 2)
        assert not ws.closed
        peer.send("after timeout")
        assert await asyncio.wait_for(call(ws.next_payload), 2) == "after timeout"


@pytest.mark.asyncio
async def test_buffered_transport_input(websocket_connection: Any, tls: bool) -> None:
    async with websocket_connection(tls) as (ws, peer, call, response):
        # One TLS record / asyncio buffer contains far more than one backend read.
        response._fp.from_promise._conn.blocksize = 64
        payload = b"x" * 8000
        peer.send(payload)
        assert await asyncio.wait_for(call(ws.next_payload), 5) == payload


@pytest.mark.asyncio
async def test_message_buffered_with_handshake(
    websocket_connection: Any, tls: bool
) -> None:
    async with websocket_connection(tls, initial_message="already here") as (
        ws,
        _,
        call,
        _,
    ):
        assert await asyncio.wait_for(call(ws.next_payload), 2) == "already here"


@pytest.mark.asyncio
@pytest.mark.parametrize("buffered", [False, True])
async def test_control_frames_between_messages(
    websocket_connection: Any, tls: bool, buffered: bool
) -> None:
    async with websocket_connection(tls) as (ws, peer, call, _):
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
    websocket_connection: Any, tls: bool, frames: bytes, expected: str | bytes
) -> None:
    async with websocket_connection(tls) as (ws, peer, call, _):
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
    websocket_connection: Any, tls: bool, frame: bytes
) -> None:
    async with websocket_connection(tls) as (ws, peer, call, _):
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
    websocket_connection: Any, tls: bool
) -> None:
    async with websocket_connection(tls) as (ws, peer, call, _):
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
async def test_read_deadline_includes_reacquiring_connection(
    websocket_connection: Any, tls: bool
) -> None:
    from threading import Event

    async with websocket_connection(tls, timeout=0.2) as (ws, peer, call, response):
        import pytest

        monkeypatch = pytest.MonkeyPatch()
        waiting = observe_wait(monkeypatch, False, ws)
        read = asyncio.ensure_future(call(ws.next_payload))
        await asyncio.wait_for(waiting.get(), 2)
        holding = asyncio.Event()
        release: Any = Event()
        loop = asyncio.get_running_loop()
        police = ws._police_officer

        def sync_hold() -> Any:
            with police.borrow(response):
                loop.call_soon_threadsafe(holding.set)
                assert release.wait(3)

        hold = sync_hold

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
    websocket_connection: Any, tls: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Event

    async with websocket_connection(tls) as (ws, peer, call, response):
        ready = asyncio.Event()
        entered = asyncio.Event()
        release: Any = Event()
        loop = asyncio.get_running_loop()
        original = ws._wait_for_read

        def sync_wait(sock: Any) -> Any:
            loop.call_soon_threadsafe(entered.set)
            original(sock)
            loop.call_soon_threadsafe(ready.set)
            assert release.wait(3)

        wait = sync_wait

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
    websocket_connection: Any, tls: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Event

    async with websocket_connection(tls) as (ws, peer, call, response):
        first_chunk = asyncio.Event()
        written_at = []
        original_send = ws._dsa.sendall
        original_receive = ws._protocol.receive_data
        loop = asyncio.get_running_loop()
        seen = 0
        queued = Event()
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
                assert queued.wait(2)

        monkeypatch.setattr(ws._protocol, "receive_data", receive)

        def sync_send(data: bytes) -> Any:
            written_at.append(seen)
            original_send(data)

        send = sync_send

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
    websocket_connection: Any, tls: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Event

    async with websocket_connection(tls) as (ws, peer, call, response):
        entered = asyncio.Event()
        released: Any = Event()
        original = ws._wait_for_read
        loop = asyncio.get_running_loop()

        def sync_wait(sock: Any) -> Any:
            loop.call_soon_threadsafe(entered.set)
            assert released.wait(2)
            original(sock)

        wait = sync_wait

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
    websocket_connection: Any, tls: bool
) -> None:
    async with websocket_connection(tls, timeout=0.1) as (ws, peer, call, response):
        police = ws._police_officer

        def sync_read() -> Any:
            with police.borrow(response):
                return ws.next_payload()

        read = sync_read

        with pytest.raises(ReadTimeoutError):
            await asyncio.wait_for(call(read), 1)
        assert not ws.closed
        peer.send("valid")
        assert await asyncio.wait_for(call(read), 1) == "valid"


@pytest.mark.asyncio
async def test_concurrent_close_is_idempotent(
    websocket_connection: Any, tls: bool
) -> None:
    async with websocket_connection(tls, timeout=1) as (ws, peer, call, response):
        police = ws._police_officer
        conn = response._fp.from_promise._conn
        await asyncio.wait_for(asyncio.gather(*[call(ws.close) for _ in range(8)]), 2)
        assert ws.closed
        assert id(conn) not in police._registry
        await asyncio.wait_for(peer.finished.wait(), 2)


@pytest.mark.asyncio
async def test_tls_proxy_duplex_and_close(
    websocket_connection: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with websocket_connection(True, tls_proxy=True) as (ws, peer, call, _):
        waiting = observe_wait(monkeypatch, False, ws)
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
    websocket_connection: Any,
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
    async with websocket_connection(False, timeout=5) as (ws, peer, call, response):
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
    websocket_connection: Any,
) -> None:
    async with websocket_connection(False, timeout=5) as (ws, _, call, response):
        with pytest.raises(OSError, match="already plugged in"):
            await call(response.start_extension, type(ws)())
        await call(response.close)
        assert ws.closed


@pytest.mark.asyncio
async def test_message_and_close_in_one_transport_read(
    websocket_connection: Any,
) -> None:
    async with websocket_connection(False, timeout=5) as (ws, peer, call, _):
        assert peer.writer is not None
        peer.writer.write(peer.encode("last message") + b"\x88\x02\x03\xe8")
        assert await call(ws.next_payload) == "last message"
        assert await call(ws.next_payload) is None
        assert ws.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handshake_error", ["missing-accept", "wrong-accept", "unknown-extension"]
)
async def test_invalid_handshake_closes_transport(
    websocket_connection: Any,
    tls: bool,
    handshake_error: str,
) -> None:
    with pytest.raises(ProtocolError):
        async with websocket_connection(
            tls, timeout=5, handshake_error=handshake_error
        ):
            pytest.fail("Invalid WebSocket handshake was accepted")


@pytest.mark.asyncio
async def test_negotiated_compression_and_fragmented_messages(
    websocket_connection: Any,
    tls: bool,
) -> None:
    from urllib3 import HttpVersion

    async with websocket_connection(
        tls, timeout=5, compression=True, initial_message="ready"
    ) as (ws, peer, call, response):
        assert "permessage-deflate" in response.headers["sec-websocket-extensions"]
        assert ws.headers(HttpVersion.h11)["sec-websocket-version"] == "13"
        assert await call(ws.next_payload) == "ready"
        for message in ("compressed text " * 32, b"compressed bytes " * 32):
            half = len(message) // 2
            assert peer.writer is not None
            peer.writer.write(
                peer.encode("first")
                + peer.encode(message[:half], message_finished=False)
                + peer.encode(message[half:])
                + peer.encode("next")
            )
            assert await call(ws.next_payload) == "first"
            assert await call(ws.next_payload) == message
            assert await call(ws.next_payload) == "next"
        await call(ws.send_payload, "client message")
        assert await asyncio.wait_for(peer.received.get(), 5) == "client message"


@pytest.mark.asyncio
async def test_malformed_compressed_frame_closes_transport(
    websocket_connection: Any,
    tls: bool,
) -> None:
    async with websocket_connection(tls, timeout=5, compression=True) as (
        ws,
        peer,
        call,
        _,
    ):
        assert peer.writer is not None
        # RSV1 marks compression; BTYPE=3 is invalid in a DEFLATE block.
        peer.writer.write(b"\xc1\x01\x07")
        try:
            assert await call(ws.next_payload) is None
        except ProtocolError:
            pass
        assert ws.closed


@pytest.mark.asyncio
async def test_close_with_two_queued_readers(
    websocket_connection: Any,
    tls: bool,
) -> None:
    async with websocket_connection(tls, timeout=5) as (ws, _, call, _):
        loop = asyncio.get_running_loop()
        acquiring: asyncio.Queue[None] = asyncio.Queue()
        lock = ws._read_lock
        if lock is None:
            lock = asyncio.Lock()

        class ObservedLock:
            def acquire(self) -> Any:
                # Signal after next_payload's initial closed check. Both readers
                # must have entered before close; the real lock still serializes them.
                loop.call_soon_threadsafe(acquiring.put_nowait, None)
                return lock.acquire()

            def release(self) -> None:
                lock.release()

        ws._read_lock = ObservedLock()
        readers = []
        try:
            for _ in range(2):
                readers.append(asyncio.ensure_future(call(ws.next_payload)))
                await asyncio.wait_for(acquiring.get(), 5)
            await asyncio.wait_for(call(ws.close), 5)
            assert await asyncio.wait_for(asyncio.gather(*readers), 5) == [None, None]
            assert ws.closed
        finally:
            await call(ws.close)
            await asyncio.gather(*readers, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["initial-read", "ready-read", "second-close"])
async def test_close_while_waiting_for_connection_ownership(
    websocket_connection: Any, phase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with websocket_connection(False, timeout=5) as (ws, peer, call, response):
        queued = observe_websocket_borrows(ws, False, monkeypatch)
        tasks = []
        try:
            if phase == "ready-read":
                waiting = observe_wait(monkeypatch, False, ws)
                tasks.append(asyncio.ensure_future(call(ws.next_payload)))
                await asyncio.wait_for(waiting.get(), 5)
            async with held_websocket_connection(ws, response, call, False):
                if phase == "ready-read":
                    peer.send("ready")
                else:
                    tasks.append(
                        asyncio.ensure_future(
                            call(
                                ws.close if phase == "second-close" else ws.next_payload
                            )
                        )
                    )
                await asyncio.wait_for(queued.get(), 5)
                tasks.append(asyncio.ensure_future(call(ws.close)))
                await asyncio.wait_for(queued.get(), 5)
            assert await asyncio.wait_for(asyncio.gather(*tasks), 5) == [None, None]
            assert ws.closed
        finally:
            await call(ws.close)
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_deadline_expiring_after_readiness_keeps_unread_message(
    websocket_connection: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib
    import time
    from types import SimpleNamespace

    async with websocket_connection(False, timeout=5) as (ws, peer, call, _):
        waiting = observe_wait(monkeypatch, False, ws)
        original = ws._before_read_pick
        expired = None

        def before_pick(conn: Any) -> Any:
            nonlocal expired
            result = original(conn)
            if result is None and ws._read_deadline is not None:
                expired = ws._read_deadline + 1
            return result

        with monkeypatch.context() as clock:
            clock.setattr(
                importlib.import_module(type(ws).__module__),
                "time",
                SimpleNamespace(
                    monotonic=lambda: time.monotonic() if expired is None else expired
                ),
            )
            clock.setattr(ws, "_before_read_pick", before_pick)
            reader = asyncio.ensure_future(call(ws.next_payload))
            await asyncio.wait_for(waiting.get(), 5)
            peer.send("still buffered")
            with pytest.raises(ReadTimeoutError):
                await asyncio.wait_for(reader, 5)
        assert not ws.closed
        assert await call(ws.next_payload) == "still buffered"


@pytest.mark.asyncio
async def test_missing_connection_is_not_reported_as_clean_websocket_eof(
    websocket_connection: Any,
) -> None:
    from urllib3.util.traffic_police import UnavailableTraffic

    async with websocket_connection(False, timeout=5) as (ws, _, call, response):
        police = ws._police_officer
        conn = response._fp.from_promise._conn
        await call(police.kill, conn)
        with pytest.raises(UnavailableTraffic):
            await call(ws.next_payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["write", "ping"])
async def test_protocol_encoder_failure_closes_websocket(
    websocket_connection: Any, operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with websocket_connection(False, timeout=5) as (ws, _, call, response):
        fast = hasattr(ws._protocol, "send_text")
        if fast:
            from websockets.exceptions import InvalidState

            error: Exception = InvalidState("injected encoder failure")
            method = "send_text" if operation == "write" else "send_ping"
        else:
            from wsproto.utilities import LocalProtocolError

            error = LocalProtocolError("injected encoder failure")
            method = "send"
        original = getattr(ws._protocol, method)

        def fail_once(*args: Any, **kwargs: Any) -> Any:
            monkeypatch.setattr(ws._protocol, method, original)
            raise error

        monkeypatch.setattr(ws._protocol, method, fail_once)
        with pytest.raises(ProtocolError) as caught:
            if operation == "write":
                await call(ws.send_payload, "message")
            else:
                await call(ws.ping)
        assert caught.value.__cause__ is error
        assert ws.closed and response._fp.from_promise._conn.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("after_input", [False, True])
async def test_wsproto_internal_assertion_is_translated_and_closed(
    websocket_connection: Any,
    after_input: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if websocket_connection.keywords["implementation"] != "wsproto":
        pytest.skip("wsproto assertion contract")
    async with websocket_connection(False, timeout=5) as (ws, peer, call, _):
        events = ws._protocol.events
        received = False
        receive = ws._protocol.receive_data

        def observed_input(data: Any) -> Any:
            nonlocal received
            result = receive(data)
            received = True
            return result

        def fail_once() -> Any:
            if received or not after_input:
                monkeypatch.setattr(ws._protocol, "events", events)
                raise AssertionError("injected parser invariant failure")
            return events()

        monkeypatch.setattr(ws._protocol, "receive_data", observed_input)
        monkeypatch.setattr(ws._protocol, "events", fail_once)
        peer.send("wire input")
        with pytest.raises(ProtocolError) as caught:
            await call(ws.next_payload)
        assert isinstance(caught.value.__cause__, AssertionError)
        assert ws.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["closing-write", "shutdown"])
async def test_websocket_close_tolerates_socket_failure(
    websocket_connection: Any, phase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with websocket_connection(False, timeout=5) as (ws, _, call, response):
        conn = response._fp.from_promise._conn
        sock = conn.sock
        if phase == "closing-write":
            target, method = ws._dsa, "sendall"
        else:
            target, method = type(sock), "shutdown"
        original = getattr(target, method)
        injected = False

        def fail_once(*args: Any, **kwargs: Any) -> Any:
            nonlocal injected
            if not injected and (phase != "shutdown" or args[0] is sock):
                injected = True
                raise OSError("injected close failure")
            return original(*args, **kwargs)

        with monkeypatch.context() as failure:
            failure.setattr(target, method, fail_once)
            await call(ws.close)
        assert injected and ws.closed and conn.sock is None


@pytest.mark.asyncio
async def test_closed_readiness_error_wakes_reader(
    websocket_connection: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    async with websocket_connection(False, timeout=5) as (ws, _, call, response):
        waiting = observe_wait(monkeypatch, False, ws)
        sock = response._fp.from_promise._conn.sock
        module = importlib.import_module(type(ws).__module__)
        sync_wait = module.wait_for_read

        def wait_sync(current: Any, *args: Any) -> Any:
            result = sync_wait(current, *args)
            if current is sock and ws._read_closed:
                raise OSError("readiness socket closed")
            return result

        monkeypatch.setattr(module, "wait_for_read", wait_sync)
        reader = asyncio.ensure_future(call(ws.next_payload))
        try:
            await asyncio.wait_for(waiting.get(), 5)
            await call(ws.close)
            assert await asyncio.wait_for(reader, 5) is None
        finally:
            await call(ws.close)
            await asyncio.gather(reader, return_exceptions=True)


@pytest.mark.asyncio
async def test_small_reads_use_buffered_inner_tls_plaintext(
    websocket_connection: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib3.util.ssltransport import SSLTransport

    async with websocket_connection(True, timeout=5, tls_proxy=True, blocksize=64) as (
        ws,
        peer,
        call,
        _,
    ):
        before_pick = ws._before_read_pick
        buffered = 0

        def observe(conn: Any) -> Any:
            nonlocal buffered
            sock = conn.sock
            if isinstance(sock, SSLTransport) and (
                sock.sslobj.pending() or sock.incoming.pending
            ):
                buffered += 1
            return before_pick(conn)

        monkeypatch.setattr(ws, "_before_read_pick", observe)
        peer.send("x" * 2048)
        assert await call(ws.next_payload) == "x" * 2048
        assert buffered > 0


@pytest.mark.asyncio
async def test_multiplexed_websocket_queued_close_is_idempotent(
    http3_peer: HTTP3Peer, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("wsproto")
    from urllib3 import HTTPSConnectionPool, HttpVersion
    from urllib3.contrib.webextensions.ws import WebSocketExtensionFromMultiplexedHTTP

    async def call(fn: Any, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(
            None, functools.partial(fn, *args, **kwargs)
        )

    pool = HTTPSConnectionPool(
        "localhost",
        http3_peer.port,
        ca_cert_data=http3_peer.ca_data,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        disabled_svn={HttpVersion.h11, HttpVersion.h2},
        timeout=5,
        retries=False,
        background_watch_delay=None,
    )
    ws = WebSocketExtensionFromMultiplexedHTTP()
    closers = []
    try:
        response = await call(
            pool.request, "GET", "/", extension=ws, preload_content=False
        )
        assert response.version == 30 and response.status == 200
        queued = observe_websocket_borrows(ws, False, monkeypatch)
        async with held_websocket_connection(ws, response, call, False):
            for _ in range(2):
                closers.append(asyncio.ensure_future(call(ws.close)))
                await asyncio.wait_for(queued.get(), 5)
        await asyncio.wait_for(asyncio.gather(*closers), 5)
        assert ws.closed
        assert (await call(pool.request, "GET", "/next")).status == 200
    finally:
        await call(ws.close)
        await asyncio.gather(*closers, return_exceptions=True)
        await call(pool.close)
