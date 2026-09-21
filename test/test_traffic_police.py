from __future__ import annotations

import asyncio
from concurrent.futures import Future
from threading import Event, Thread, current_thread
from typing import Any

import pytest

from urllib3 import HTTPResponse
from urllib3._async.response import AsyncHTTPResponse
from urllib3.util._async.traffic_police import AsyncTrafficPolice
from urllib3.util.traffic_police import (
    OverwhelmedTraffic,
    TrafficPolice,
    UnavailableTraffic,
)


class Connection:
    def __init__(self, saturated: bool = False) -> None:
        self.is_saturated = saturated
        self.is_idle = not saturated
        self.closed = False

    def close(self) -> None:
        self.closed = True


class AsyncConnection(Connection):
    async def close(self) -> None:  # type: ignore[override]
        super().close()


@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("callback_raises", [False, True])
def test_locate_readiness_releases_connection(
    held: bool, callback_raises: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    police: TrafficPolice[Any] = TrafficPolice(maxsize=1)
    conn, indicator = Connection(), HTTPResponse()
    police.put(conn, indicator, immediately_unavailable=held)
    registered, writer_done = Event(), Event()
    result: Future[Any] = Future()
    register = police._register_signal
    calls = 0

    def on_register(*args: Any) -> Any:
        signal = register(*args)
        registered.set()
        return signal

    monkeypatch.setattr(police, "_register_signal", on_register)

    def wait() -> None:
        assert not police.busy

        def write() -> None:
            with police.borrow(indicator):
                writer_done.set()

        writer = Thread(target=write, daemon=True)
        writer.start()
        writer.join(2)
        assert not writer.is_alive()
        assert writer_done.is_set()

    def before_pick(candidate: Connection) -> Any:
        nonlocal calls
        calls += 1
        assert candidate is conn
        if callback_raises:
            raise ValueError("readiness inspection failed")
        return wait if calls == 1 else None

    def read() -> None:
        try:
            result.set_result(
                police.locate(indicator, timeout=1, conn_pre_pick_callable=before_pick)
            )
        except BaseException as exc:
            if police.busy:
                result.set_exception(
                    AssertionError("Failed readiness check retained ownership")
                )
            else:
                result.set_exception(exc)
        finally:
            police.release()

    reader = Thread(target=read, daemon=True)
    reader.start()
    try:
        if held:
            assert registered.wait(2)
    finally:
        police.release()
    reader.join(2)
    assert not reader.is_alive()
    if callback_raises:
        with pytest.raises(ValueError, match="readiness inspection failed"):
            result.result()
        assert calls == 1
    else:
        assert result.result() is conn
        assert calls == 2
    assert not police._cursor
    with police.borrow(indicator) as available:
        assert available is conn


@pytest.mark.asyncio
@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("callback_raises", [False, True])
async def test_async_locate_readiness_releases_connection(
    held: bool, callback_raises: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    police: AsyncTrafficPolice[Any] = AsyncTrafficPolice(maxsize=1)
    conn, indicator = AsyncConnection(saturated=True), AsyncHTTPResponse()
    await police.put(conn, indicator, immediately_unavailable=True)
    if not held:
        police.release()
    registered = asyncio.Event()
    register = police._signals.register
    calls = 0

    def on_register(*args: Any) -> Any:
        signal = register(*args)
        registered.set()
        return signal

    monkeypatch.setattr(police._signals, "register", on_register)

    async def wait() -> None:
        assert not police.busy

        async def write() -> None:
            async with police.borrow(indicator) as available:
                assert available is conn

        await asyncio.wait_for(asyncio.create_task(write()), 1)

    def before_pick(candidate: AsyncConnection) -> Any:
        nonlocal calls
        calls += 1
        assert candidate is conn
        if callback_raises:
            raise ValueError("readiness inspection failed")
        return wait if calls == 1 else None

    async def read() -> Any:
        try:
            return await police.locate(
                indicator, timeout=1, conn_pre_pick_callable=before_pick
            )
        except BaseException:
            assert not police.busy
            raise
        finally:
            police.release()

    reader = asyncio.create_task(read())
    try:
        if held:
            await asyncio.wait_for(registered.wait(), 2)
    finally:
        police.release()
    if callback_raises:
        with pytest.raises(ValueError, match="readiness inspection failed"):
            await asyncio.wait_for(reader, 2)
        assert calls == 1
    else:
        assert await asyncio.wait_for(reader, 2) is conn
        assert calls == 2
    assert not police._cursors
    async with police.borrow(indicator) as available:
        assert available is conn


@pytest.mark.parametrize("operation", ["locate", "locate_none", "factory"])
@pytest.mark.parametrize(
    "concurrency, held",
    [(False, False), (True, False), (False, True)],
    ids=["exclusive", "shared", "queued"],
)
def test_locate_registers_borrower_or_waiter_before_unlock(
    operation: str, concurrency: bool, held: bool
) -> None:
    police: TrafficPolice[Any] = TrafficPolice(maxsize=1, concurrency=concurrency)
    conn = Connection()
    indicator = HTTPResponse()
    police.put(conn, indicator, immediately_unavailable=held)
    unlocked, resume = Event(), Event()
    result: Future[Any] = Future()
    lock = police._lock

    class LookupGate:
        armed = True

        def acquire(self) -> None:
            lock.acquire()

        def release(self) -> None:
            lock.release()
            if current_thread() is reader and self.armed:
                self.armed = False
                unlocked.set()
                assert resume.wait(5)

        def __enter__(self) -> None:
            self.acquire()

        def __exit__(self, *args: Any) -> None:
            self.release()

    def locate() -> None:
        try:
            if operation == "factory":
                with police.locate_or_hold(indicator) as found:
                    result.set_result(found)
            elif operation == "locate_none":
                result.set_result(
                    police.locate(indicator, timeout=0.2, conn_pre_pick_callable=None)
                )
            else:
                result.set_result(police.locate(indicator, timeout=0.2))
        except BaseException as exc:
            result.set_exception(exc)
        finally:
            police.release()

    police._lock = LookupGate()  # type: ignore[assignment]
    reader = Thread(target=locate)
    reader.start()
    try:
        assert unlocked.wait(5)
        if held:
            with lock:
                assert len(police._signals) == 1
                assert police._signals[0].target_conn_or_pool is conn
        else:
            with lock:
                reader_ident = reader.ident
                assert reader_ident is not None
                assert police._cursor[reader_ident].conn_or_pool is conn
            with pytest.raises(OverwhelmedTraffic):
                police._sacrifice_first_idle(block=False)
            assert not conn.closed
    finally:
        if held:
            police.kill_cursor()
        resume.set()
        reader.join(5)
    assert not reader.is_alive()
    if held:
        with pytest.raises(UnavailableTraffic):
            result.result()
    else:
        assert result.result() is conn
        police._sacrifice_first_idle(block=False)
        assert conn.closed
    assert not police._registry
    assert not police._signals


@pytest.mark.parametrize("maxsize", [2, 3])
@pytest.mark.parametrize("write", [False, True])
def test_get_with_saturated_available_and_held_idle_connection(
    maxsize: int, write: bool
) -> None:
    police: TrafficPolice[Any] = TrafficPolice(maxsize=maxsize)
    available, held = Connection(saturated=True), Connection()
    police.put(available)
    police.put(held, immediately_unavailable=True)
    result: Future[Any] = Future()

    def acquire() -> None:
        try:
            result.set_result(police.get(block=False, non_saturated_only=write))
        except BaseException as exc:
            result.set_exception(exc)
        finally:
            police.release()

    thread = Thread(target=acquire)
    thread.start()
    thread.join(5)
    assert not thread.is_alive()
    if write and maxsize == 2:
        with pytest.raises(UnavailableTraffic):
            result.result()
    else:
        assert result.result() is (None if write else available)
    assert police.is_held(held)
    assert len(police._registry) <= maxsize
    police.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("maxsize", [2, 3])
@pytest.mark.parametrize("write", [False, True])
async def test_async_get_with_saturated_available_and_held_idle_connection(
    maxsize: int, write: bool
) -> None:
    police: AsyncTrafficPolice[Any] = AsyncTrafficPolice(maxsize=maxsize)
    available, held = AsyncConnection(saturated=True), AsyncConnection()
    await police.put(available, immediately_unavailable=True)
    police.release()
    await police.put(held, immediately_unavailable=True)

    async def acquire() -> Any:
        try:
            return await police.get(block=False, non_saturated_only=write)
        finally:
            police.release()

    task = asyncio.create_task(acquire())
    if write and maxsize == 2:
        with pytest.raises(UnavailableTraffic):
            await task
    else:
        assert await task is (None if write else available)
    assert police.is_held(held)
    assert len(police._registry) <= maxsize
    police.release()


def test_shared_pool_eviction_waits_for_last_borrower(monkeypatch: Any) -> None:
    police: TrafficPolice[Any] = TrafficPolice(maxsize=1, concurrency=True)
    first, replacement = Connection(), Connection()
    indicator = HTTPResponse()
    police.put(first, indicator, immediately_unavailable=True)
    borrowed, resume, queued = Event(), Event(), Event()
    borrower_result: Future[Any] = Future()
    eviction_result: Future[Any] = Future()
    register = police._register_signal

    def registered(*args: Any) -> Any:
        signal = register(*args)
        queued.set()
        return signal

    monkeypatch.setattr(police, "_register_signal", registered)

    def borrow() -> None:
        try:
            with police.borrow(indicator):
                borrowed.set()
                assert resume.wait(5)
            borrower_result.set_result(None)
        except BaseException as exc:
            borrower_result.set_exception(exc)

    def evict() -> None:
        try:
            police.put(replacement, immediately_unavailable=True)
            police.release()
            eviction_result.set_result(None)
        except BaseException as exc:
            eviction_result.set_exception(exc)

    borrower = Thread(target=borrow)
    evictor = Thread(target=evict, daemon=True)
    borrower.start()
    try:
        assert borrowed.wait(5)
        evictor.start()
        assert queued.wait(1)
        police.release()
        assert not first.closed
        assert not eviction_result.done()
    finally:
        police.release()
        resume.set()
        borrower.join(5)
        if evictor.ident is not None:
            evictor.join(5)
    borrower_result.result()
    eviction_result.result(timeout=1)
    assert first.closed
    assert not replacement.closed
    assert list(police._registry.values()) == [replacement]
    assert list(police._container.values()) == [replacement]
    assert not police._cursor
    assert not police._signals


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [None, "queued", "notified"])
async def test_async_shared_pool_eviction_waits_for_last_borrower(
    cancel: str | None,
) -> None:
    police: AsyncTrafficPolice[Any] = AsyncTrafficPolice(maxsize=1, concurrency=True)
    first, replacement = AsyncConnection(), AsyncConnection()
    indicator = AsyncHTTPResponse()
    await police.put(first, indicator, immediately_unavailable=True)
    borrowed, resume = asyncio.Event(), asyncio.Event()

    async def borrow() -> None:
        async with police.borrow(indicator):
            borrowed.set()
            await resume.wait()

    async def evict() -> None:
        await police.put(replacement, immediately_unavailable=True)
        police.release()

    borrower = asyncio.create_task(borrow())
    await borrowed.wait()
    evictor = asyncio.create_task(evict())
    try:
        await asyncio.sleep(0)
        assert not first.closed
        assert not evictor.done()
        assert police._signals._priority_signals
        if cancel == "queued":
            evictor.cancel()
        # Another borrower still owns the idle pool.
        resume.set()
        await borrower
        assert not first.closed
        police.release()
        if cancel == "notified":
            evictor.cancel()
        if cancel is not None:
            with pytest.raises(asyncio.CancelledError):
                await evictor
            assert not first.closed
            evictor = asyncio.create_task(evict())
        await asyncio.wait_for(evictor, 1)
        assert first.closed
        assert not replacement.closed
        assert list(police._registry.values()) == [replacement]
        assert list(police._container.values()) == [replacement]
        assert not police._cursors
        assert not police._signals._priority_signals
        assert not police._signals._writing_tasks
    finally:
        resume.set()
        police.release()
        for task in (borrower, evictor):
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_pool_reservation_releases_lookup_lock_while_waiting(
    monkeypatch: Any,
) -> None:
    police: TrafficPolice[Any] = TrafficPolice(maxsize=1, concurrency=True)
    first, replacement = Connection(), Connection()
    ready, resume, queued = Event(), Event(), Event()
    owner_result: Future[Any] = Future()
    factory_result: Future[Any] = Future()
    register = police._register_signal

    def registered(*args: Any) -> Any:
        signal = register(*args)
        queued.set()
        return signal

    monkeypatch.setattr(police, "_register_signal", registered)

    def own() -> None:
        try:
            police.put(first, immediately_unavailable=True)
            ready.set()
            assert resume.wait(5)
            police.release()
            owner_result.set_result(None)
        except BaseException as exc:
            owner_result.set_exception(exc)

    def create() -> None:
        try:
            with police.locate_or_hold(HTTPResponse()) as swap:
                swap(replacement)
            police.release()
            factory_result.set_result(None)
        except BaseException as exc:
            factory_result.set_exception(exc)

    owner = Thread(target=own, daemon=True)
    factory = Thread(target=create, daemon=True)
    owner.start()
    try:
        assert ready.wait(5)
        factory.start()
        assert queued.wait(5)
        assert not first.closed
    finally:
        resume.set()
        owner.join(2)
        if factory.ident is not None:
            factory.join(2)
    owner_result.result(timeout=1)
    factory_result.result(timeout=1)
    assert first.closed
    assert list(police._registry.values()) == [replacement]
    assert not police._cursor
    assert not police._signals


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_index", [0, 1])
async def test_cancelling_notified_evictor_does_not_strand_other_waiters(
    cancel_index: int,
) -> None:
    police: AsyncTrafficPolice[Any] = AsyncTrafficPolice(maxsize=1, concurrency=True)
    first = AsyncConnection()
    await police.put(first, immediately_unavailable=True)

    async def evict() -> None:
        await police.put(AsyncConnection(), immediately_unavailable=True)
        police.release()

    tasks = [asyncio.create_task(evict()) for _ in range(2)]
    try:
        await asyncio.sleep(0)
        police.release()
        tasks[cancel_index].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[cancel_index]
        await asyncio.wait_for(tasks[1 - cancel_index], 1)
        assert first.closed
        assert len(police._registry) == 1
        assert not police._cursors
        assert not police._signals._priority_signals
    finally:
        police.release()
        for task in tasks:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_zero_sized_pool_reservation_preserves_legacy_behavior() -> None:
    police: TrafficPolice[Any] = TrafficPolice(maxsize=0)
    conn = Connection()
    with police.locate_or_hold(HTTPResponse(), block=False) as swap:
        swap(conn)
    assert police.is_held(conn)
    police.release()
