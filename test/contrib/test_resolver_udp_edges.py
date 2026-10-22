from __future__ import annotations

import asyncio
import inspect
import socket
from time import monotonic
from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident
from typing import Any

import pytest

from test.conftest import DNSUDPServer
from urllib3.contrib.resolver.dou import PlainResolver
from urllib3.contrib.resolver._async.dou import PlainResolver as AsyncPlainResolver
from urllib3.contrib.resolver.protocols import SupportedQueryType
from urllib3.contrib.resolver.utils import _udp_retry_deadlines


async def call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    result = fn(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_udp_transaction_capacity_refusal_preserves_resolver(
    dns_udp_server: DNSUDPServer, asynchronous: bool
) -> None:
    resolver: Any = (AsyncPlainResolver if asynchronous else PlainResolver)(
        *dns_udp_server.address, timeout=5
    )
    try:
        with pytest.raises(socket.gaierror, match="ID space exhausted"):
            resolver._reserve_queries(
                "too-many.example", [SupportedQueryType.A] * 65537
            )
        assert not resolver._pending
        records = await call(
            resolver.getaddrinfo,
            "next.example",
            443,
            socket.AF_INET,
            socket.SOCK_STREAM,
        )
        assert records[0][4] == ("192.0.2.1", 443)
    finally:
        await call(resolver.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("dns_udp_server", ["https-ech"], indirect=True)
@pytest.mark.parametrize("socket_kind", [0, socket.SOCK_RAW])
async def test_udp_other_socket_kinds_do_not_gain_quic_records(
    dns_udp_server: DNSUDPServer, asynchronous: bool, socket_kind: socket.SocketKind
) -> None:
    resolver: Any = (AsyncPlainResolver if asynchronous else PlainResolver)(
        *dns_udp_server.address, timeout=5
    )
    try:
        records = await call(
            resolver.getaddrinfo,
            "kind.example",
            443,
            socket.AF_INET,
            socket_kind,
            quic_upgrade_via_dns_rr=True,
        )
        assert records and all(record[1] == socket_kind for record in records)
    finally:
        await call(resolver.close)


@pytest.mark.parametrize("operation", ["send", "receive", "send-timeout"])
def test_udp_recovers_from_socket_would_block(
    dns_udp_server: DNSUDPServer, operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib3.contrib.resolver.dou import _socket

    resolver = PlainResolver(*dns_udp_server.address, timeout=5)
    sock = resolver._socket
    method = (
        "sendall"
        if operation.startswith("send")
        else ("recvmsg" if resolver._gro_enabled else "recv")
    )
    original = getattr(type(sock), method)
    failures = 0
    expired = False

    def write_deadline_reached(*args: Any) -> bool:
        nonlocal expired
        expired = True
        return False

    def would_block(current: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal failures
        if current is sock and (not failures or operation == "send-timeout"):
            failures += 1
            raise BlockingIOError("injected UDP backpressure")
        return original(current, *args, **kwargs)

    try:
        with monkeypatch.context() as pressure:
            pressure.setattr(type(sock), method, would_block)
            if operation == "send-timeout":
                # Let setup finish, then expire this resolver's clock exactly
                # when the injected readiness wait reports its timeout.
                pressure.setattr(_socket, "wait_for_write", write_deadline_reached)
                pressure.setattr(
                    _socket,
                    "monotonic",
                    lambda: float("inf") if expired else monotonic(),
                )
                pressure.setattr(
                    _socket, "_udp_retry_deadlines", lambda deadline: iter([deadline])
                )
                with pytest.raises(socket.gaierror, match="timed out"):
                    resolver.getaddrinfo(
                        "blocked.example", 443, socket.AF_INET, socket.SOCK_STREAM
                    )
            else:
                assert resolver.getaddrinfo(
                    "blocked.example", 443, socket.AF_INET, socket.SOCK_STREAM
                )
        assert failures
        assert not resolver._pending and not resolver._completed
        resolver._timeout = 5
        assert resolver.getaddrinfo(
            "next.example", 443, socket.AF_INET, socket.SOCK_STREAM
        )
    finally:
        resolver.close()


def test_udp_read_ownership_timeout_does_not_strand_queries(
    dns_udp_server: DNSUDPServer,
) -> None:
    resolver = PlainResolver(*dns_udp_server.address, timeout=0.03)
    try:
        with resolver._read_lock:
            with ThreadPoolExecutor(max_workers=1) as executor:
                task = executor.submit(
                    resolver.getaddrinfo,
                    "held.example",
                    443,
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                )
                with pytest.raises(socket.gaierror, match="timed out"):
                    task.result(5)
        assert not resolver._pending and not resolver._completed
        resolver._timeout = 5
        assert resolver.getaddrinfo(
            "next.example", 443, socket.AF_INET, socket.SOCK_STREAM
        )
    finally:
        resolver.close()


@pytest.mark.parametrize(
    "dns_udp_server,checkpoint",
    [("normal", "sent"), ("normal", "locked"), ("timeout", "deadline")],
    indirect=["dns_udp_server"],
)
def test_udp_close_at_exchange_checkpoints(
    dns_udp_server: DNSUDPServer, checkpoint: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib3.contrib.resolver.dou import _socket

    resolver = PlainResolver(
        *dns_udp_server.address, timeout=0.03 if checkpoint == "deadline" else 5
    )
    sock = resolver._socket
    if checkpoint == "sent":
        sendall = type(sock).sendall
        sends = 0

        def sent(current: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal sends
            result = sendall(current, *args, **kwargs)
            if current is sock:
                sends += 1
                if sends == 2:
                    resolver.close()
            return result

        monkeypatch.setattr(type(sock), "sendall", sent)
    elif checkpoint == "locked":
        lock = resolver._read_lock

        class CloseOnAcquire:
            def acquire(self, **kwargs: Any) -> bool:
                acquired = lock.acquire(**kwargs)
                if acquired:
                    resolver.close()
                return acquired

            def release(self) -> None:
                lock.release()

        monkeypatch.setattr(resolver, "_read_lock", CloseOnAcquire())
    else:
        deadlines = _udp_retry_deadlines

        def finish(deadline: float) -> Any:
            yield from deadlines(deadline)
            resolver.close()

        monkeypatch.setattr(_socket, "_udp_retry_deadlines", finish)
    try:
        with pytest.raises(socket.gaierror, match="disconnected"):
            resolver.getaddrinfo(
                "closing.example", 443, socket.AF_INET, socket.SOCK_STREAM
            )
        assert not resolver._pending and not resolver._completed
        assert sock.fileno() == -1
    finally:
        resolver.close()


@pytest.mark.asyncio
async def test_async_udp_zero_deadline_does_not_start_connection(
    dns_udp_server: DNSUDPServer,
) -> None:
    resolver = AsyncPlainResolver(*dns_udp_server.address, timeout=0)
    try:
        with pytest.raises(socket.gaierror, match="timed out"):
            await resolver.getaddrinfo(
                "expired.example", 443, socket.AF_INET, socket.SOCK_STREAM
            )
        assert resolver._socket is None and not dns_udp_server.requests
    finally:
        await resolver.close()


@pytest.mark.asyncio
async def test_async_udp_connection_deadline_and_close(
    dns_udp_server: DNSUDPServer,
) -> None:
    connected = asyncio.Event()

    class SlowConnection(AsyncPlainResolver):
        async def _connect(self) -> None:
            await super()._connect()
            connected.set()
            await asyncio.Event().wait()

    resolver = SlowConnection(*dns_udp_server.address, timeout=5)
    connecting = asyncio.create_task(resolver._ensure_connected())
    try:
        await asyncio.wait_for(connected.wait(), 5)
        # Setup gets its own budget; the short lookup deadline starts only once
        # the real socket exists and the shared connection task is held.
        resolver._timeout = 0.03
        with pytest.raises(socket.gaierror, match="timed out"):
            await asyncio.wait_for(
                resolver.getaddrinfo(
                    "connecting.example", 443, socket.AF_INET, socket.SOCK_STREAM
                ),
                5,
            )
        sock = resolver._socket
        assert sock is not None
        task = resolver._connection_task
        assert task is not None
        await resolver.close()
        assert sock.fileno() == -1
        assert task.cancelled()
    finally:
        connecting.cancel()
        await asyncio.gather(connecting, return_exceptions=True)
        await resolver.close()


@pytest.mark.asyncio
async def test_async_udp_close_after_queries_are_sent(
    dns_udp_server: DNSUDPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolver = AsyncPlainResolver(*dns_udp_server.address, timeout=5)
    await resolver._ensure_connected()
    sock = resolver._socket
    assert sock is not None
    sendall = type(sock).sendall
    sends = 0

    async def sent(current: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal sends
        await sendall(current, *args, **kwargs)
        if current is sock:
            sends += 1
            if sends == 2:
                await resolver.close()

    monkeypatch.setattr(type(sock), "sendall", sent)
    try:
        with pytest.raises(socket.gaierror, match="disconnected"):
            await resolver.getaddrinfo(
                "closing.example", 443, socket.AF_INET, socket.SOCK_STREAM
            )
        assert not resolver._pending and not resolver._completed
    finally:
        await resolver.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dns_udp_server,checkpoint",
    [("normal", "locked"), ("timeout", "deadline")],
    indirect=["dns_udp_server"],
)
async def test_async_udp_close_at_read_checkpoints(
    dns_udp_server: DNSUDPServer, checkpoint: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib3.contrib.resolver._async.dou import _socket
    from urllib3.contrib.ssa._timeout import timeout

    resolver = AsyncPlainResolver(*dns_udp_server.address, timeout=5)
    await resolver._ensure_connected()
    deadlines: list[Any] = []
    if checkpoint == "deadline":

        def arm_at_read_checkpoint(delay: float) -> Any:
            deadline = timeout(None)
            deadlines.append(deadline)
            return deadline

        monkeypatch.setattr(_socket, "timeout_", arm_at_read_checkpoint)
        monkeypatch.setattr(
            _socket, "_udp_retry_deadlines", lambda deadline: iter([deadline])
        )
    lock = resolver._read_semaphore

    class Gate:
        async def __aenter__(self) -> None:
            await lock.acquire()
            if checkpoint == "locked":
                await resolver.close()
            else:
                deadlines[-1].reschedule(asyncio.get_running_loop().time())

        async def __aexit__(self, kind: Any, error: Any, tb: Any) -> None:
            lock.release()
            if checkpoint == "deadline" and isinstance(error, asyncio.CancelledError):
                await resolver.close()

    monkeypatch.setattr(resolver, "_read_semaphore", Gate())
    try:
        with pytest.raises(socket.gaierror, match="disconnected"):
            await resolver.getaddrinfo(
                "closing.example", 443, socket.AF_INET, socket.SOCK_STREAM
            )
        assert not resolver._pending and not resolver._completed
    finally:
        await resolver.close()


@pytest.mark.parametrize("dns_udp_server", ["reverse-two-lookups"], indirect=True)
def test_udp_answer_collected_at_read_lock_deadline(
    dns_udp_server: DNSUDPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib3.contrib.resolver.dou import _socket

    first_reading, second_waiting, answers_ready = Event(), Event(), Event()
    second_owner = None
    second_ids: list[int] = []
    dns_udp_server.respond.clear()

    class Resolver(PlainResolver):
        def _reserve_queries(self, host: str, query_types: Any) -> Any:
            nonlocal second_owner, second_ids
            queries = super()._reserve_queries(host, query_types)
            if host == "second.example":
                second_owner = get_ident()
                second_ids = [q.id for q in queries]
            return queries

    resolver = Resolver(*dns_udp_server.address, timeout=5)
    lock = resolver._read_lock
    clock = monotonic

    class Gate:
        def acquire(self, **kwargs: Any) -> bool:
            if get_ident() == second_owner:
                # The first lookup owns the actual lock until the peer can
                # respond. Advance only this waiter's deadline after that
                # reader has parsed its answers from the real UDP packets.
                assert not lock.acquire(blocking=False)
                second_waiting.set()
                assert answers_ready.wait(5)
                return False
            acquired = lock.acquire(**kwargs)
            if acquired:
                first_reading.set()
            return acquired

        def release(self) -> None:
            if second_ids and all(i in resolver._completed for i in second_ids):
                answers_ready.set()
            lock.release()

    monkeypatch.setattr(resolver, "_read_lock", Gate())
    monkeypatch.setattr(
        _socket,
        "monotonic",
        lambda: (
            float("inf")
            if get_ident() == second_owner and answers_ready.is_set()
            else clock()
        ),
    )
    monkeypatch.setattr(
        _socket, "_udp_retry_deadlines", lambda deadline: iter([deadline])
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                resolver.getaddrinfo,
                "first.example",
                443,
                socket.AF_INET,
                socket.SOCK_STREAM,
            )
            assert first_reading.wait(5)
            second = executor.submit(
                resolver.getaddrinfo,
                "second.example",
                443,
                socket.AF_INET,
                socket.SOCK_STREAM,
            )
            try:
                assert second_waiting.wait(5)
            finally:
                dns_udp_server.respond.set()
            assert second.result(5)[0][4] == ("192.0.2.1", 443)
            assert first.result(5)[0][4] == ("192.0.2.1", 443)
        assert not resolver._pending and not resolver._completed
    finally:
        dns_udp_server.respond.set()
        resolver.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("dns_udp_server", ["reverse-two-lookups"], indirect=True)
async def test_async_udp_answer_collected_at_read_lock_deadline(
    dns_udp_server: DNSUDPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib3.contrib.resolver._async.dou import _socket

    first_reading, second_waiting, answers_ready = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    second_owner = None
    second_ids: list[int] = []
    dns_udp_server.respond.clear()

    class Resolver(AsyncPlainResolver):
        def _reserve_queries(self, host: str, query_types: Any) -> Any:
            nonlocal second_owner, second_ids
            queries = super()._reserve_queries(host, query_types)
            if host == "second.example":
                second_owner = asyncio.current_task()
                second_ids = [q.id for q in queries]
            return queries

    resolver = Resolver(*dns_udp_server.address, timeout=5)
    lock = resolver._read_semaphore

    class Gate:
        async def __aenter__(self) -> None:
            if asyncio.current_task() is second_owner:
                assert lock.locked()
                second_waiting.set()
                await answers_ready.wait()
                raise TimeoutError("read ownership deadline")
            await lock.acquire()
            first_reading.set()

        async def __aexit__(self, *args: Any) -> None:
            if second_ids and all(i in resolver._completed for i in second_ids):
                answers_ready.set()
            lock.release()

    monkeypatch.setattr(resolver, "_read_semaphore", Gate())
    monkeypatch.setattr(
        _socket, "_udp_retry_deadlines", lambda deadline: iter([deadline])
    )
    tasks: list[asyncio.Future[Any]] = []
    try:
        tasks.append(
            asyncio.ensure_future(
                resolver.getaddrinfo(
                    "first.example", 443, socket.AF_INET, socket.SOCK_STREAM
                )
            )
        )
        await asyncio.wait_for(first_reading.wait(), 5)
        tasks.append(
            asyncio.ensure_future(
                resolver.getaddrinfo(
                    "second.example", 443, socket.AF_INET, socket.SOCK_STREAM
                )
            )
        )
        await asyncio.wait_for(second_waiting.wait(), 5)
        dns_udp_server.respond.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 5)
        assert all(records[0][4] == ("192.0.2.1", 443) for records in results)
        assert not resolver._pending and not resolver._completed
    finally:
        dns_udp_server.respond.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await resolver.close()
