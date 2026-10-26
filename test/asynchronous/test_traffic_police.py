from __future__ import annotations

import asyncio
from typing import Any

import pytest

from test.traffic_police_helpers import (
    AsyncConnection,
    AsyncTraffic,
    ObservedAsyncPolice,
)
from urllib3._async.response import AsyncHTTPResponse
from urllib3.backend import ResponsePromise
from urllib3.util._async.traffic_police import AsyncTrafficPolice
from urllib3.util.traffic_police import (
    AtomicTraffic,
    ItemPlaceholder,
    OverwhelmedTraffic,
    TrafficState,
    UnavailableTraffic,
)


async def store(police: Any, traffic: Any, *markers: Any) -> None:
    await police.put(traffic, *markers, immediately_unavailable=True)
    police.release()


@pytest.mark.asyncio
async def test_memorize_during_borrow_does_not_reserve_checkout() -> None:
    police: AsyncTrafficPolice[Any] = AsyncTrafficPolice()
    conn: Any = AsyncConnection()
    marker = AsyncHTTPResponse()
    await police.put(conn, marker)
    async with police.borrow(marker):
        response = AsyncHTTPResponse(connection=conn, police_officer=police)
        assert await police.beacon(response)
        assert police._reclaim_reservation(response) is None
        assert police.is_held(conn)


@pytest.mark.asyncio
async def test_reserving_forgotten_response_does_not_restore_route() -> None:
    police: AsyncTrafficPolice[Any] = AsyncTrafficPolice()
    conn, response = AsyncConnection(), AsyncHTTPResponse()
    await police.put(conn, response, immediately_unavailable=True)
    police.forget(response)
    police.memorize(response, conn, reserve=True)
    assert not await police.beacon(response)
    assert police._reclaim_reservation(response) is None
    police.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("unrelated_checkout", [False, True])
async def test_reclaim_response_reservation(unrelated_checkout: bool) -> None:
    police: AsyncTrafficPolice[Any] = AsyncTrafficPolice()
    conn, other, response = AsyncConnection(), AsyncConnection(), AsyncHTTPResponse()

    async def reserve() -> None:
        await police.put(conn, response, immediately_unavailable=True)
        police.memorize(response, conn, reserve=True)

    owner = asyncio.create_task(reserve())
    await owner
    if unrelated_checkout:
        await police.put(other, immediately_unavailable=True)
    assert police._reclaim_reservation(response) is (
        None if unrelated_checkout else conn
    )
    assert police.is_held(other if unrelated_checkout else conn)
    assert not police._reclaim_reservation(response)
    police._signals.terminate_writing()
    police.release()
    if unrelated_checkout:
        assert police._reclaim_reservation(response)
        police._signals.terminate_writing()
        police.release()
    assert not police._cursors
    assert not police._signals._writing_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ["borrow", "borrow_none", "locate", "release", "put"]
)
async def test_intervening_operation_invalidates_reservation(operation: str) -> None:
    police: AsyncTrafficPolice[Any] = AsyncTrafficPolice()
    conn, response, neighbour = (
        AsyncConnection(),
        AsyncHTTPResponse(),
        AsyncHTTPResponse(),
    )
    await police.put(conn, response, neighbour, immediately_unavailable=True)
    police.memorize(response, conn, reserve=True)
    if operation in ("borrow", "borrow_none"):
        async with police.borrow(neighbour if operation == "borrow" else None):
            police.memorize(neighbour, conn, reserve=True)
            assert not police._reclaim_reservation(response)
            assert not police._reclaim_reservation(neighbour)
            police.release()
    elif operation == "locate":
        await police.locate(neighbour)
        police.release()
    elif operation == "put":
        await police.put(conn)
    else:
        police.release()
    assert not police._reclaim_reservation(response)
    police.release()
    assert not police._cursors


@pytest.mark.asyncio
async def test_handoff_does_not_inherit_response_reservation() -> None:
    police = ObservedAsyncPolice()
    conn, response, neighbour = (
        AsyncConnection(),
        AsyncHTTPResponse(),
        AsyncHTTPResponse(),
    )
    await police.put(conn, response, neighbour, immediately_unavailable=True)
    police.memorize(response, conn, reserve=True)
    acquired, finish = asyncio.Event(), asyncio.Event()

    async def read() -> None:
        async with police.borrow(neighbour):
            acquired.set()
            await asyncio.wait_for(finish.wait(), 5)
            assert police.is_held(conn)

    reader = asyncio.create_task(read())
    try:
        await asyncio.wait_for(police.queued.get(), 5)
        police.release()
        await asyncio.wait_for(acquired.wait(), 5)
        assert not police._reclaim_reservation(response)
    finally:
        finish.set()
        await asyncio.wait_for(reader, 5)
    assert not police._cursors


@pytest.mark.asyncio
async def test_async_kill_returned_target_preserves_unrelated_cursor() -> None:
    police: AsyncTrafficPolice[Any] = AsyncTrafficPolice()
    target, other = AsyncConnection(), AsyncConnection(saturated=True)
    await police.put(target, immediately_unavailable=True)
    police.release()
    await police.put(other, immediately_unavailable=True)
    assert await police.kill(target, idle_only=True)
    assert target.closed and not other.closed
    assert police.is_held(other)
    assert id(target) not in police._registry and id(target) not in police._container
    assert not await police.kill(target, idle_only=True)
    await police.kill_cursor()
    assert other.closed and not police.busy
    assert police.rsize() == police.qsize() == 0
    assert not police._signals._writing_tasks


@pytest.mark.asyncio
async def test_async_kill_idle_waits_for_nested_borrows() -> None:
    police: AsyncTrafficPolice[Any] = AsyncTrafficPolice()
    target, indicator = AsyncConnection(), AsyncHTTPResponse()
    await police.put(target, indicator, immediately_unavailable=True)
    police.release()
    async with police.borrow(indicator):
        async with police.borrow(indicator):
            assert not await police.kill(target, idle_only=True)
        assert not await police.kill(target, idle_only=True)
        assert not target.closed
    assert await police.kill(target, idle_only=True)
    assert target.closed and not police._map


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [False, True])
@pytest.mark.parametrize("wire_finished", [False, True])
async def test_async_shutdown_waits_for_last_borrow(
    concurrency: bool, wire_finished: bool
) -> None:
    police: AsyncTrafficPolice[Any] = AsyncTrafficPolice(concurrency=concurrency)
    target, indicator = AsyncConnection(saturated=True), AsyncHTTPResponse()
    await police.put(target, indicator, immediately_unavailable=True)
    if wire_finished:
        setattr(target, "_pending_responses", {1: indicator})
    police.release()
    acquired, release = asyncio.Event(), asyncio.Event()

    async def borrow() -> None:
        async with police.borrow(indicator):
            async with police.borrow(indicator):
                acquired.set()
                await release.wait()
            assert not target.closed

    worker = asyncio.create_task(borrow())
    try:
        await asyncio.wait_for(acquired.wait(), 5)
        await police.clear()
        target.is_saturated, target.is_idle = False, True
        if wire_finished:
            # The backend has received EOF, but the response still owns its borrow.
            setattr(target, "_pending_responses", {})
        await police.clear()
        assert not target.closed
        release.set()
        await asyncio.wait_for(worker, 5)
        assert target.closed
        assert police.rsize() == police.qsize() == 0
        assert not police._shutdown
    finally:
        release.set()
        await asyncio.wait_for(worker, 5)


@pytest.mark.asyncio
async def test_cancelled_borrow_during_shutdown_preserves_cancellation() -> None:
    police: AsyncTrafficPolice[Any] = AsyncTrafficPolice()
    target, indicator = AsyncConnection(saturated=True), AsyncHTTPResponse()
    await police.put(target, indicator, immediately_unavailable=True)
    await police.clear()

    async def borrow() -> None:
        async with police.borrow(indicator):
            pytest.fail("The connection is still held by another task")

    worker = asyncio.create_task(borrow())
    try:
        await asyncio.sleep(0)
        assert police._signals._furthest_signals
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await police.kill_cursor()


@pytest.mark.asyncio
async def test_async_shutdown_disposes_idle_target_after_borrow_error() -> None:
    police: AsyncTrafficPolice[Any] = AsyncTrafficPolice()
    target, indicator = AsyncConnection(saturated=True), AsyncHTTPResponse()
    await police.put(target, indicator, immediately_unavailable=True)
    police.release()
    with pytest.raises(ValueError, match="response processing failed"):
        async with police.borrow(indicator):
            await police.clear()
            target.is_saturated, target.is_idle = False, True
            raise ValueError("response processing failed")
    assert target.closed
    assert police.rsize() == police.qsize() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["release", "drain", "close"])
async def test_async_borrow_by_type_wakes_queued_readers(
    finish: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    police: AsyncTrafficPolice[Any] = AsyncTrafficPolice(maxsize=1)
    conn, indicator = AsyncConnection(saturated=True), AsyncHTTPResponse()
    await police.put(conn, indicator, immediately_unavailable=True)
    queued = asyncio.Event()
    register = police._signals.register
    waiter_count = 0

    def on_register(*args: Any) -> Any:
        nonlocal waiter_count
        signal = register(*args)
        waiter_count += 1
        if waiter_count == 3:
            queued.set()
        return signal

    monkeypatch.setattr(police._signals, "register", on_register)

    async def read() -> Any:
        try:
            async with police.borrow(AsyncHTTPResponse) as available:
                assert available is conn
                assert police.is_held(conn)
                return available
        except UnavailableTraffic:
            return None

    readers = [asyncio.create_task(read()) for _ in range(3)]
    try:
        await asyncio.wait_for(queued.wait(), 1)
        if finish == "drain":
            police.forget(indicator)
            conn.is_saturated = False
            conn.is_idle = True
        if finish == "close":
            await police.kill_cursor()
        else:
            police.release()
        results = await asyncio.wait_for(asyncio.gather(*readers), 1)
        assert results == [conn if finish == "release" else None] * 3
        assert list(police._container.values()) == ([] if finish == "close" else [conn])
        assert not police._cursors
        assert not police._signals._furthest_signals
    finally:
        police.release()
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("notified", [False, True])
async def test_async_cancelled_type_borrow_does_not_lose_connection(
    notified: bool,
) -> None:
    police: AsyncTrafficPolice[Any] = AsyncTrafficPolice(maxsize=1)
    conn, indicator = AsyncConnection(saturated=True), AsyncHTTPResponse()
    await police.put(conn, indicator, immediately_unavailable=True)

    async def read() -> Any:
        async with police.borrow(AsyncHTTPResponse) as available:
            return available

    readers = [asyncio.create_task(read()) for _ in range(2)]
    try:
        # A single checkpoint lets both readers register before release.
        await asyncio.sleep(0)
        assert len(police._signals._furthest_signals) == 2
        if notified:
            police.release()
        readers[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await readers[0]
        if not notified:
            police.release()
        assert await asyncio.wait_for(readers[1], 1) is conn
        assert list(police._container.values()) == [conn]
        assert not police._cursors
        assert not police._signals._furthest_signals
    finally:
        police.release()
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("state", list(TrafficState))
@pytest.mark.parametrize("concurrency", [False, True])
async def test_state_views_and_selection(
    state: TrafficState, concurrency: bool
) -> None:
    police: Any = AsyncTrafficPolice(concurrency=concurrency)
    traffic: Any = AsyncTraffic(state)
    assert police.rsize() == police.qsize() == 0
    assert not police.bag_only_saturated
    assert not police.bag_not_saturated
    await store(police, traffic)
    assert police.bag_only_idle is (state is TrafficState.IDLE)
    assert police.bag_only_saturated is (state is TrafficState.SATURATED)
    assert police.bag_not_saturated is (state is not TrafficState.SATURATED)
    assert state.name.title() in repr(police)
    assert await police.get_nowait() is traffic
    assert police.is_held(traffic)
    assert police.qsize() == int(concurrency)
    police.release()
    assert not police.busy
    assert police.qsize() == 1
    traffic.state = TrafficState.IDLE
    await police.clear()
    assert traffic.close_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("filter_name", ["non_saturated_only", "not_idle_only"])
@pytest.mark.parametrize("concurrency", [False, True])
async def test_selection_filters_skip_ineligible_objects(
    filter_name: str, concurrency: bool
) -> None:
    police: Any = AsyncTrafficPolice(concurrency=concurrency)
    rejected_state = (
        TrafficState.SATURATED
        if filter_name == "non_saturated_only"
        else TrafficState.IDLE
    )
    rejected: Any = AsyncTraffic(rejected_state)
    accepted: Any = AsyncTraffic(TrafficState.USED)
    await store(police, rejected)
    await store(police, accepted)
    assert await police.get_nowait(**{filter_name: True}) is accepted
    police.release()
    await police.kill(accepted)
    with pytest.raises(UnavailableTraffic):
        await police.get_nowait(**{filter_name: True})
    assert not police.busy
    assert police.rsize() == police.qsize() == 1
    await police.kill(rejected)


@pytest.mark.asyncio
async def test_argument_and_ownership_errors_preserve_current_checkout() -> None:
    police: Any = AsyncTrafficPolice()
    first, second = AsyncTraffic(), AsyncTraffic()
    a, b = object(), object()
    await store(police, first, a)
    await store(police, second, b)
    with pytest.raises(AtomicTraffic):
        police.memorize(object())
    with pytest.raises(ValueError, match="unsupported traffic_indicator"):
        await police.locate(object)
    assert await police.locate(a) is first
    with pytest.raises(AtomicTraffic):
        await police.get_nowait()
    with pytest.raises(AtomicTraffic):
        await police.locate(b)
    with pytest.raises(AtomicTraffic):
        async for _ in police.iter_idle():
            pytest.fail("iteration must reject existing ownership")
    assert police.is_held(first)
    police.release()
    assert await police.locate(b) is second
    police.release()
    await police.clear()
    assert first.close_count == second.close_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [False, True])
async def test_duplicate_put_is_idempotent(concurrency: bool) -> None:
    police: Any = AsyncTrafficPolice(concurrency=concurrency)
    traffic: Any = AsyncTraffic()
    await store(police, traffic)
    await police.put(traffic)
    assert police.rsize() == police.qsize() == 1
    assert not police.busy
    await police.clear()
    assert traffic.close_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("held", [False, True])
async def test_shutdown_put_disposes_finished_object(held: bool) -> None:
    police: Any = AsyncTrafficPolice()
    traffic: Any = AsyncTraffic(TrafficState.USED)
    indicator = object()
    await police.put(traffic, indicator, immediately_unavailable=True)
    await police.clear()
    assert police._shutdown and traffic.close_count == 0
    if not held:
        police.release()
    traffic.state = TrafficState.IDLE
    await police.put(traffic)
    assert traffic.close_count == 1
    assert not police.busy and not police._map
    assert police.rsize() == police.qsize() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_close", [False, True])
async def test_capacity_eviction_cleans_routes_even_if_close_fails(
    fail_close: bool,
) -> None:
    police: Any = AsyncTrafficPolice(maxsize=1)
    old, new = AsyncTraffic(), AsyncTraffic()
    old.fail_close = fail_close
    marker = object()
    await store(police, old, marker)
    await police.put(new, block=False, immediately_unavailable=True)
    police.release()
    assert old.close_count == 1 and new.close_count == 0
    assert not await police.beacon(marker)
    assert police.rsize() == police.qsize() == 1
    assert await police.get_nowait() is new
    police.release()
    await police.clear()


@pytest.mark.asyncio
async def test_capacity_refuses_discarding_used_object() -> None:
    police: Any = AsyncTrafficPolice(maxsize=1)
    old: Any = AsyncTraffic(TrafficState.USED)
    new: Any = AsyncTraffic()
    await store(police, old)
    with pytest.raises(OverwhelmedTraffic):
        await police.put(new, block=False)
    assert police.rsize() == police.qsize() == 1
    assert old.close_count == new.close_count == 0
    await police.kill(old)
    await police._sacrifice_first_idle(block=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [False, True])
async def test_nested_borrow_without_indicator(concurrency: bool) -> None:
    police: Any = AsyncTrafficPolice(concurrency=concurrency)
    traffic: Any = AsyncTraffic()
    await store(police, traffic)
    async with police.borrow() as outer:
        async with police.borrow() as inner:
            assert outer is inner is traffic
        assert police.is_held(traffic)
    assert not police.busy and police.qsize() == 1
    await police.clear()


@pytest.mark.asyncio
async def test_empty_borrow_returns_its_reservation() -> None:
    police: Any = AsyncTrafficPolice(maxsize=1)
    with pytest.raises(UnavailableTraffic):
        async with police.borrow(block=False):
            pytest.fail("no object is registered")
    assert not police.busy
    await police.clear()


@pytest.mark.asyncio
async def test_beacon_finds_indicator_in_child_police() -> None:
    parent: Any = AsyncTrafficPolice(concurrency=True)
    child: Any = AsyncTrafficPolice()
    owner: Any = AsyncTraffic()
    connection = AsyncTraffic()
    owner.pool = child
    child.parent = parent
    marker = object()
    await store(child, connection, marker)
    await store(parent, owner)
    assert await parent.locate(marker) is owner
    parent.release()
    assert await child.beacon(marker)
    child.forget(marker)
    assert not await parent.locate(marker)
    await child.clear()
    await parent.clear()


@pytest.mark.asyncio
async def test_tuple_indicator_and_readiness_rejection() -> None:
    police: Any = AsyncTrafficPolice()
    traffic: Any = AsyncTraffic(TrafficState.USED)
    marker = ("host", 443)
    await store(police, traffic, marker)
    assert await police.beacon(marker)
    assert await police.beacon(tuple)
    assert not police.busy
    with pytest.raises(UnavailableTraffic, match="Candidate is not ready"):
        await police.locate(
            marker,
            block=False,
            conn_pre_pick_callable=lambda _: lambda: None,
        )
    assert not police.busy and police.qsize() == 1
    assert await police.locate(marker) is traffic
    police.release()
    police.forget(marker)
    assert not await police.beacon(marker)
    await police.kill(traffic)


@pytest.mark.asyncio
async def test_shared_police_rejects_readiness_callback() -> None:
    police: Any = AsyncTrafficPolice(concurrency=True)
    with pytest.raises(ValueError, match="exclusive ownership"):
        await police.locate(object(), conn_pre_pick_callable=lambda _: None)
    assert not police.busy


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [False, True])
async def test_idle_iterator_skips_disconnected_objects(concurrency: bool) -> None:
    police: Any = AsyncTrafficPolice(concurrency=concurrency)
    dead, live = AsyncTraffic(), AsyncTraffic()
    dead.is_connected = False
    await store(police, dead)
    await store(police, live)
    selected = [item async for item in police.iter_idle()]
    assert selected == [live]
    assert dead.close_count == 1 and live.close_count == 0
    assert not police.busy
    await police.clear()


@pytest.mark.asyncio
async def test_abandoned_reservation_is_reclaimed() -> None:
    police: Any = AsyncTrafficPolice(maxsize=1)
    marker = ("host", 443)
    async with police.locate_or_hold(marker):
        assert police.busy
    assert not police.busy and police.rsize() == 0
    assert not await police.beacon(marker)


@pytest.mark.asyncio
async def test_empty_tracks_registered_objects() -> None:
    police: Any = AsyncTrafficPolice()
    assert police.empty()
    traffic: Any = AsyncTraffic()
    await store(police, traffic)
    assert not police.empty()
    await police.clear()
    assert police.empty()


@pytest.mark.asyncio
async def test_new_put_makes_object_available() -> None:
    police: Any = AsyncTrafficPolice()
    traffic: Any = AsyncTraffic()
    marker = object()
    await police.put(traffic, marker)
    assert police.rsize() == police.qsize() == 1
    assert await police.get_nowait() is traffic
    police.release()
    assert await police.beacon(marker)
    await police.clear()
    assert traffic.close_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("forget_wanted", [False, True])
async def test_async_type_lookup_rechecks_an_unrelated_handoff(
    forget_wanted: bool,
) -> None:
    police = ObservedAsyncPolice()
    wanted, other = AsyncTraffic(TrafficState.USED), AsyncTraffic(TrafficState.USED)
    marker: Any = object()
    other_marker: Any = ("other",)
    await store(police, other, other_marker)
    await police.put(wanted, marker, immediately_unavailable=True)
    other_held, return_other = asyncio.Event(), asyncio.Event()

    async def hold_other() -> None:
        await police.locate(other_marker)
        other_held.set()
        try:
            await return_other.wait()
        finally:
            police.release()

    async def find() -> Any:
        try:
            return await police._find_by(object)
        finally:
            police.release()

    holder = asyncio.create_task(hold_other())
    reader = None
    try:
        await asyncio.wait_for(other_held.wait(), 5)
        reader = asyncio.create_task(find())
        await asyncio.wait_for(police.queued.get(), 5)
        if forget_wanted:
            police.forget(marker)
        return_other.set()
        await asyncio.wait_for(holder, 5)
        if not forget_wanted:
            await asyncio.wait_for(police.queued.get(), 5)
            police.release()
        assert await asyncio.wait_for(reader, 5) is (None if forget_wanted else wanted)
    finally:
        return_other.set()
        police.release()
        if reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)
    assert not police._cursors and not police._signals._furthest_signals
    assert police.rsize() == police.qsize() == 2
    await police.kill(wanted)
    await police.kill(other)


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [None, 5.0])
async def test_async_placeholder_resolves_to_registered_object(
    timeout: float | None,
) -> None:
    police = ObservedAsyncPolice(maxsize=1)
    marker: Any = ("host", 443)
    traffic: Any = AsyncTraffic()
    entered = asyncio.Event()

    async def locate() -> Any:
        entered.set()
        try:
            return await police.locate(marker, timeout=timeout)
        finally:
            police.release()

    async with police.locate_or_hold(marker) as replace:
        reader = asyncio.create_task(locate())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            # locate has yielded while the placeholder still owns the route.
            assert not reader.done()
            assert callable(replace)
            await replace(traffic)
            police.release()
            assert await asyncio.wait_for(reader, 5) is traffic
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
    await police.clear()
    assert traffic.close_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "read", "locate", "beacon"])
async def test_async_queued_operations_receive_the_returned_object(
    operation: str,
) -> None:
    police = ObservedAsyncPolice(maxsize=1)
    traffic: Any = AsyncTraffic(TrafficState.USED)
    marker: Any = object()
    await police.put(traffic, marker, immediately_unavailable=True)

    async def wait() -> Any:
        try:
            if operation == "locate":
                return await police.locate(
                    marker, conn_pre_pick_callable=lambda _: None
                )
            if operation == "beacon":
                result = await police.beacon(object)
                assert not police.busy
                return result
            return await police.get(not_idle_only=operation == "read")
        finally:
            police.release()

    reader = asyncio.create_task(wait())
    try:
        await asyncio.wait_for(police.queued.get(), 5)
        await police.put(traffic)
        assert await asyncio.wait_for(reader, 5) is (
            True if operation == "beacon" else traffic
        )
    finally:
        police.release()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
    assert not police._cursors and police.qsize() == 1
    await police.kill(traffic)


@pytest.mark.asyncio
async def test_queued_reader_rejects_idle_handoff() -> None:
    police = ObservedAsyncPolice(maxsize=1)
    traffic = AsyncTraffic(TrafficState.USED)
    await police.put(traffic, immediately_unavailable=True)

    async def read() -> Any:
        try:
            return await police.get(not_idle_only=True)
        finally:
            police.release()

    reader = asyncio.create_task(read())
    try:
        signal = await asyncio.wait_for(police.queued.get(), 5)
        traffic.state = TrafficState.IDLE
        police.release()
        assert not signal.event.is_set()
        assert not reader.done()
        assert police.qsize() == 1

        assert await police.get_nowait() is traffic
        traffic.state = TrafficState.USED
        police.release()
        assert await asyncio.wait_for(reader, 5) is traffic
        assert not police._cursors and police.qsize() == 1
    finally:
        police.release()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await police.kill(traffic)


@pytest.mark.asyncio
async def test_async_idle_iterator_rechecks_state_after_yield() -> None:
    police = AsyncTrafficPolice[Any]()
    first, second = AsyncTraffic(), AsyncTraffic()
    await store(police, first)
    await store(police, second)
    selected = []
    async for traffic in police.iter_idle():
        selected.append(traffic)
        second.state = TrafficState.USED
    assert selected == [first]
    assert not police.busy
    assert police.qsize() == police.rsize() == 2
    await police.kill(first)
    await police.kill(second)


@pytest.mark.asyncio
async def test_async_shared_put_returns_existing_checkout() -> None:
    police = AsyncTrafficPolice[Any](concurrency=True)
    reservation = ItemPlaceholder()
    await police.put(reservation, immediately_unavailable=True)
    assert police.busy and police.qsize() == 0
    await police.put(reservation)
    assert not police.busy
    await police.kill(reservation)


@pytest.mark.asyncio
async def test_async_signal_contracts_and_priority() -> None:
    police = AsyncTrafficPolice[Any]()
    signals = police._signals
    traffic: Any = AsyncTraffic(TrafficState.USED)
    assert not signals.terminate_writing()
    with pytest.raises(AtomicTraffic):
        signals.next()
    await police.put(traffic, immediately_unavailable=True)
    # Register a pending writer through the signal API, then declare the current
    # operation as a writer so readers must respect writer priority.
    signal = signals.register(traffic, TrafficState.IDLE, TrafficState.USED)
    signals.declare_writing()
    signals._refresh_generic_waiters()
    assert signals.should_queue_read_operation(traffic)
    traffic.state = TrafficState.SATURATED
    assert not signals.should_queue_read_operation(traffic)
    signals.next()
    traffic.state = TrafficState.USED
    assert not signals.should_queue_read_operation(traffic)
    signals.unregister(signal)
    signals.terminate_writing()
    assert not signals.should_queue_read_operation(traffic)
    police.release()
    await police.kill(traffic)


@pytest.mark.asyncio
@pytest.mark.parametrize("already_finished", [False, True])
async def test_async_shutdown_put_wakes_reader(already_finished: bool) -> None:
    police = ObservedAsyncPolice()
    traffic: Any = AsyncTraffic(TrafficState.USED)
    marker: Any = object()
    await police.put(traffic, marker, immediately_unavailable=True)

    async def read() -> None:
        async with police.borrow(marker) as found:
            assert found is traffic
            traffic.state = TrafficState.IDLE

    reader = asyncio.create_task(read())
    try:
        await asyncio.wait_for(police.queued.get(), 5)
        await police.clear()
        if already_finished:
            traffic.state = TrafficState.IDLE
        await police.put(traffic)
        # A registered indicator remains locatable when the object becomes
        # idle; its reader rechecks completion after acquisition. The final
        # borrower still performs shutdown disposal exactly once.
        await asyncio.wait_for(reader, 5)
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
    assert traffic.close_count == 1
    assert not police.busy and not police._map
    assert police.rsize() == police.qsize() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("callback_raises", [False, True])
async def test_async_queued_readiness_yields_to_another_reader(
    callback_raises: bool,
) -> None:
    police = ObservedAsyncPolice()
    traffic: Any = AsyncTraffic(TrafficState.SATURATED)
    marker: Any = object()
    await police.put(traffic, marker, immediately_unavailable=True)
    second_finished = asyncio.Event()
    calls = 0

    async def wait() -> None:
        assert not police.busy
        await second_finished.wait()

    def before_pick(candidate: Any) -> Any:
        nonlocal calls
        assert candidate is traffic
        calls += 1
        if callback_raises:
            raise ValueError("inspection failed")
        return wait if calls == 1 else None

    async def first() -> Any:
        try:
            return await police.locate(marker, conn_pre_pick_callable=before_pick)
        finally:
            police.release()

    async def second() -> None:
        async with police.borrow(marker) as found:
            assert found is traffic
        second_finished.set()

    reader = asyncio.create_task(first())
    other = None
    try:
        await asyncio.wait_for(police.queued.get(), 5)
        other = asyncio.create_task(second())
        await asyncio.wait_for(police.queued.get(), 5)
        police.release()
        if callback_raises:
            with pytest.raises(ValueError, match="inspection failed"):
                await asyncio.wait_for(reader, 5)
        else:
            assert await asyncio.wait_for(reader, 5) is traffic
        await asyncio.wait_for(other, 5)
        assert second_finished.is_set()
    finally:
        police.release()
        reader.cancel()
        if other is not None:
            other.cancel()
            await asyncio.gather(other, return_exceptions=True)
        await asyncio.gather(reader, return_exceptions=True)
    assert not police._cursors
    await police.kill(traffic)


@pytest.mark.asyncio
async def test_async_put_rejects_another_tasks_checkout() -> None:
    police = AsyncTrafficPolice[Any]()
    first, second = AsyncTraffic(), AsyncTraffic()
    a: Any = object()
    b: Any = object()
    await store(police, second, b)
    await police.put(first, a, immediately_unavailable=True)
    held, finish = asyncio.Event(), asyncio.Event()

    async def owner() -> None:
        await police.locate(b)
        held.set()
        try:
            await finish.wait()
        finally:
            police.release()

    worker = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(held.wait(), 5)
        with pytest.raises(AtomicTraffic):
            await police.put(second, immediately_unavailable=True)
        assert police.is_held(first)
        police.release()
        with pytest.raises(UnavailableTraffic):
            await police.locate(b, block=False)
    finally:
        finish.set()
        await asyncio.wait_for(worker, 5)
    await police.clear()
    assert first.close_count == second.close_count == 1


@pytest.mark.asyncio
async def test_type_borrow_rejects_a_different_existing_owner() -> None:
    police: Any = AsyncTrafficPolice()
    first, second = AsyncTraffic(), AsyncTraffic()
    await store(police, first, object())
    await store(police, second, ("other",))
    assert await police.get_nowait() is second
    with pytest.raises(AtomicTraffic):
        async with police.borrow(object):
            pytest.fail("another object is already held")
    police.release()
    assert police.rsize() == police.qsize() == 2
    await police.clear()
    assert first.close_count == second.close_count == 1


@pytest.mark.asyncio
async def test_failed_pool_close_does_not_allow_reacquiring_retired_owner() -> None:
    parent: Any = AsyncTrafficPolice(concurrency=True)
    child: Any = AsyncTrafficPolice()
    owner: Any = AsyncTraffic()
    owner.pool = child
    owner.fail_close = True
    marker = ResponsePromise(owner, 1, [])
    await store(parent, owner, marker)
    await parent.clear()
    assert owner.close_count == 1 and parent.rsize() == 0
    with pytest.raises(UnavailableTraffic):
        await parent.locate(marker, block=False)
    parent.forget(marker)
    assert not parent._map and not parent.busy


@pytest.mark.asyncio
async def test_async_forget_tolerates_an_unavailable_custom_parent() -> None:
    class UnavailableParent(AsyncTrafficPolice[Any]):
        def forget(self, marker: Any) -> None:
            super().forget(marker)
            raise UnavailableTraffic("parent unavailable")

    parent = UnavailableParent()
    child = AsyncTrafficPolice[Any]()
    child.parent = parent
    traffic: Any = AsyncTraffic()
    marker: Any = object()
    await store(child, traffic, marker)
    child.forget(marker)
    assert not child._map
    assert child.qsize() == 1
    await child.clear()


@pytest.mark.asyncio
async def test_async_specific_waiter_wakes_when_owner_finishes() -> None:
    police = ObservedAsyncPolice()
    traffic: Any = AsyncTraffic(TrafficState.USED)
    marker: Any = object()
    await police.put(traffic, marker, immediately_unavailable=True)

    async def reader() -> Any:
        try:
            return await police.locate(marker)
        finally:
            police.release()

    waiter = asyncio.create_task(reader())
    try:
        await asyncio.wait_for(police.queued.get(), 5)
        traffic.state = TrafficState.IDLE
        police.release()
        assert await asyncio.wait_for(waiter, 5) is traffic
        assert not police._signals._furthest_signals
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        await police.kill(traffic)
