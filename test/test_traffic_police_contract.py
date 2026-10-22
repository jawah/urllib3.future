from __future__ import annotations

import asyncio
import inspect
from queue import Queue
from threading import Event
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from urllib3.backend import ResponsePromise
from urllib3.util._async.traffic_police import AsyncSignals, AsyncTrafficPolice
from urllib3.util.traffic_police import (
    AtomicTraffic,
    ItemPlaceholder,
    OverwhelmedTraffic,
    TrafficPolice,
    TrafficState,
    UnavailableTraffic,
)


class Traffic:
    """A managed object whose state changes independently of its checkout."""

    def __init__(self, state: TrafficState = TrafficState.IDLE) -> None:
        self.state = state
        self.is_connected = True
        self.close_count = 0
        self.fail_close = False

    @property
    def is_idle(self) -> bool:
        return self.state is TrafficState.IDLE

    @property
    def is_saturated(self) -> bool:
        return self.state is TrafficState.SATURATED

    def close(self) -> None:
        self.close_count += 1
        self.is_connected = False
        if self.fail_close:
            raise OSError("close failed")


class AsyncTraffic(Traffic):
    async def close(self) -> None:  # type: ignore[override]
        super().close()


class ObservedPolice(TrafficPolice[Any]):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.queued: Queue[Any] = Queue()

    def _register_signal(self, *args: Any) -> Any:
        signal = super()._register_signal(*args)
        self.queued.put(signal)
        return signal


class ObservedSignals(AsyncSignals[Any]):
    def __init__(self, cursors: Any, queued: asyncio.Queue[Any]) -> None:
        super().__init__(cursors)
        self.queued = queued

    def register(self, *args: Any) -> Any:
        signal = super().register(*args)
        self.queued.put_nowait(signal)
        return signal


class ObservedAsyncPolice(AsyncTrafficPolice[Any]):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.queued: asyncio.Queue[Any] = asyncio.Queue()
        self._signals = ObservedSignals(self._cursors, self.queued)


async def call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    result = fn(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


@pytest.fixture(params=[False, True], ids=["sync", "async"])
def asynchronous(request: pytest.FixtureRequest) -> bool:
    return bool(request.param)


def make_police(asynchronous: bool, **kwargs: Any) -> Any:
    return (AsyncTrafficPolice if asynchronous else TrafficPolice)(**kwargs)


def make_traffic(asynchronous: bool, state: TrafficState = TrafficState.IDLE) -> Any:
    return (AsyncTraffic if asynchronous else Traffic)(state)


async def store(police: Any, traffic: Any, *markers: Any) -> None:
    await call(police.put, traffic, *markers, immediately_unavailable=True)
    police.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", list(TrafficState))
@pytest.mark.parametrize("concurrency", [False, True])
async def test_state_views_and_selection(
    asynchronous: bool, state: TrafficState, concurrency: bool
) -> None:
    police = make_police(asynchronous, concurrency=concurrency)
    traffic = make_traffic(asynchronous, state)
    assert police.rsize() == police.qsize() == 0
    assert not police.bag_only_saturated
    if asynchronous:
        assert not police.bag_not_saturated
    await store(police, traffic)
    assert police.bag_only_idle is (state is TrafficState.IDLE)
    assert police.bag_only_saturated is (state is TrafficState.SATURATED)
    if asynchronous:
        assert police.bag_not_saturated is (state is not TrafficState.SATURATED)
    assert state.name.title() in repr(police)
    assert await call(police.get_nowait) is traffic
    assert police.is_held(traffic)
    assert police.qsize() == int(concurrency)
    police.release()
    assert not police.busy
    assert police.qsize() == 1
    traffic.state = TrafficState.IDLE
    await call(police.clear)
    assert traffic.close_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("filter_name", ["non_saturated_only", "not_idle_only"])
@pytest.mark.parametrize("concurrency", [False, True])
async def test_selection_filters_skip_ineligible_objects(
    asynchronous: bool, filter_name: str, concurrency: bool
) -> None:
    police = make_police(asynchronous, concurrency=concurrency)
    rejected_state = (
        TrafficState.SATURATED
        if filter_name == "non_saturated_only"
        else TrafficState.IDLE
    )
    rejected = make_traffic(asynchronous, rejected_state)
    accepted = make_traffic(asynchronous, TrafficState.USED)
    await store(police, rejected)
    await store(police, accepted)
    assert await call(police.get_nowait, **{filter_name: True}) is accepted
    police.release()
    await call(police.kill, accepted)
    with pytest.raises(UnavailableTraffic):
        await call(police.get_nowait, **{filter_name: True})
    assert not police.busy
    assert police.rsize() == police.qsize() == 1
    await call(police.kill, rejected)


@pytest.mark.asyncio
async def test_argument_and_ownership_errors_preserve_current_checkout(
    asynchronous: bool,
) -> None:
    police = make_police(asynchronous)
    first, second = make_traffic(asynchronous), make_traffic(asynchronous)
    a, b = object(), object()
    await store(police, first, a)
    await store(police, second, b)
    with pytest.raises(AtomicTraffic):
        police.memorize(object())
    with pytest.raises(ValueError, match="unsupported traffic_indicator"):
        await call(police.locate, object)
    assert await call(police.locate, a) is first
    with pytest.raises(AtomicTraffic):
        await call(police.get_nowait)
    with pytest.raises(AtomicTraffic):
        await call(police.locate, b)
    with pytest.raises(AtomicTraffic):
        if asynchronous:
            async for _ in police.iter_idle():
                pytest.fail("iteration must reject existing ownership")
        else:
            list(police.iter_idle())
    assert police.is_held(first)
    police.release()
    assert await call(police.locate, b) is second
    police.release()
    await call(police.clear)
    assert first.close_count == second.close_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [False, True])
async def test_duplicate_put_is_idempotent(
    asynchronous: bool, concurrency: bool
) -> None:
    police = make_police(asynchronous, concurrency=concurrency)
    traffic = make_traffic(asynchronous)
    await store(police, traffic)
    await call(police.put, traffic)
    assert police.rsize() == police.qsize() == 1
    assert not police.busy
    await call(police.clear)
    assert traffic.close_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("held", [False, True])
async def test_shutdown_put_disposes_finished_object(
    asynchronous: bool, held: bool
) -> None:
    police = make_police(asynchronous)
    traffic = make_traffic(asynchronous, TrafficState.USED)
    indicator = object()
    await call(police.put, traffic, indicator, immediately_unavailable=True)
    await call(police.clear)
    assert police._shutdown and traffic.close_count == 0
    if not held:
        police.release()
    traffic.state = TrafficState.IDLE
    await call(police.put, traffic)
    assert traffic.close_count == 1
    assert not police.busy and not police._map
    assert police.rsize() == police.qsize() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_close", [False, True])
async def test_capacity_eviction_cleans_routes_even_if_close_fails(
    asynchronous: bool, fail_close: bool
) -> None:
    police = make_police(asynchronous, maxsize=1)
    old, new = make_traffic(asynchronous), make_traffic(asynchronous)
    old.fail_close = fail_close
    marker = object()
    await store(police, old, marker)
    await call(police.put, new, block=False, immediately_unavailable=True)
    police.release()
    assert old.close_count == 1 and new.close_count == 0
    assert not await call(police.beacon, marker)
    assert police.rsize() == police.qsize() == 1
    assert await call(police.get_nowait) is new
    police.release()
    await call(police.clear)


@pytest.mark.asyncio
async def test_capacity_refuses_discarding_used_object(asynchronous: bool) -> None:
    police = make_police(asynchronous, maxsize=1)
    old = make_traffic(asynchronous, TrafficState.USED)
    new = make_traffic(asynchronous)
    await store(police, old)
    with pytest.raises(OverwhelmedTraffic):
        await call(police.put, new, block=False)
    assert police.rsize() == police.qsize() == 1
    assert old.close_count == new.close_count == 0
    await call(police.kill, old)
    await call(police._sacrifice_first_idle, block=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [False, True])
async def test_nested_borrow_without_indicator(
    asynchronous: bool, concurrency: bool
) -> None:
    police = make_police(asynchronous, concurrency=concurrency)
    traffic = make_traffic(asynchronous)
    await store(police, traffic)
    if asynchronous:
        async with police.borrow() as outer:
            async with police.borrow() as inner:
                assert outer is inner is traffic
            assert police.is_held(traffic)
    else:
        with police.borrow() as outer:
            with police.borrow() as inner:
                assert outer is inner is traffic
            assert police.is_held(traffic)
    assert not police.busy and police.qsize() == 1
    await call(police.clear)


@pytest.mark.asyncio
async def test_empty_borrow_returns_its_reservation(asynchronous: bool) -> None:
    police = make_police(asynchronous, maxsize=1)
    with pytest.raises(UnavailableTraffic):
        if asynchronous:
            async with police.borrow(block=False):
                pytest.fail("no object is registered")
        else:
            with police.borrow(block=False):
                pytest.fail("no object is registered")
    assert not police.busy
    await call(police.clear)


@pytest.mark.asyncio
async def test_beacon_finds_indicator_in_child_police(asynchronous: bool) -> None:
    parent = make_police(asynchronous, concurrency=True)
    child = make_police(asynchronous)
    owner, connection = make_traffic(asynchronous), make_traffic(asynchronous)
    owner.pool = child
    child.parent = parent
    marker = object()
    await store(child, connection, marker)
    await store(parent, owner)
    assert await call(parent.locate, marker) is owner
    parent.release()
    assert await call(child.beacon, marker)
    child.forget(marker)
    assert not await call(parent.locate, marker)
    await call(child.clear)
    await call(parent.clear)


@pytest.mark.asyncio
async def test_tuple_indicator_and_readiness_rejection(asynchronous: bool) -> None:
    police = make_police(asynchronous)
    traffic = make_traffic(asynchronous, TrafficState.USED)
    marker = ("host", 443)
    await store(police, traffic, marker)
    assert await call(police.beacon, marker)
    assert await call(police.beacon, tuple)
    assert not police.busy
    with pytest.raises(UnavailableTraffic, match="Candidate is not ready"):
        await call(
            police.locate,
            marker,
            block=False,
            conn_pre_pick_callable=lambda _: lambda: None,
        )
    assert not police.busy and police.qsize() == 1
    assert await call(police.locate, marker) is traffic
    police.release()
    police.forget(marker)
    assert not await call(police.beacon, marker)
    await call(police.kill, traffic)


@pytest.mark.asyncio
async def test_shared_police_rejects_readiness_callback(asynchronous: bool) -> None:
    police = make_police(asynchronous, concurrency=True)
    with pytest.raises(ValueError, match="exclusive ownership"):
        await call(police.locate, object(), conn_pre_pick_callable=lambda _: None)
    assert not police.busy


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [False, True])
async def test_idle_iterator_skips_disconnected_objects(
    asynchronous: bool, concurrency: bool
) -> None:
    police = make_police(asynchronous, concurrency=concurrency)
    dead, live = make_traffic(asynchronous), make_traffic(asynchronous)
    dead.is_connected = False
    await store(police, dead)
    await store(police, live)
    if asynchronous:
        selected = [item async for item in police.iter_idle()]
    else:
        selected = list(police.iter_idle())
    assert selected == [live]
    assert dead.close_count == 1 and live.close_count == 0
    assert not police.busy
    await call(police.clear)


@pytest.mark.asyncio
async def test_abandoned_reservation_is_reclaimed(asynchronous: bool) -> None:
    police = make_police(asynchronous, maxsize=1)
    marker = ("host", 443)
    if asynchronous:
        async with police.locate_or_hold(marker):
            assert police.busy
    else:
        assert not police.busy_with_placeholder
        with police.locate_or_hold(marker):
            assert police.busy_with_placeholder
    assert not police.busy and police.rsize() == 0
    assert not await call(police.beacon, marker)


@pytest.mark.asyncio
async def test_empty_tracks_registered_objects(asynchronous: bool) -> None:
    police = make_police(asynchronous)
    assert police.empty()
    traffic = make_traffic(asynchronous)
    await store(police, traffic)
    assert not police.empty()
    await call(police.clear)
    assert police.empty()


@pytest.mark.asyncio
async def test_new_put_makes_object_available(asynchronous: bool) -> None:
    police = make_police(asynchronous)
    traffic = make_traffic(asynchronous)
    marker = object()
    await call(police.put, traffic, marker)
    assert police.rsize() == police.qsize() == 1
    assert await call(police.get_nowait) is traffic
    police.release()
    assert await call(police.beacon, marker)
    await call(police.clear)
    assert traffic.close_count == 1


def test_placeholder_timeout_unregisters_waiter() -> None:
    police: TrafficPolice[Any] = TrafficPolice(maxsize=1)
    marker: Any = ("host", 443)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with police.locate_or_hold(marker):
            waiter = executor.submit(police.locate, marker, timeout=0)
            with pytest.raises(TimeoutError):
                waiter.result(5)
            assert waiter.done(), (
                "The executor watchdog must not satisfy the timeout check"
            )
            assert not police._signals
    assert not police.busy and not police._map
    assert police.rsize() == police.qsize() == 0


@pytest.mark.parametrize("forget_wanted", [False, True])
def test_type_lookup_rechecks_an_unrelated_handoff(forget_wanted: bool) -> None:
    police = ObservedPolice()
    wanted, other = Traffic(TrafficState.USED), Traffic(TrafficState.USED)
    marker: Any = object()
    other_marker: Any = ("other",)
    police.put(other, other_marker)
    police.put(wanted, marker, immediately_unavailable=True)
    other_held, return_other = Event(), Event()

    def hold_other() -> None:
        police.locate(other_marker)
        other_held.set()
        try:
            assert return_other.wait(10)
        finally:
            police.release()

    def find() -> Any:
        try:
            return police._find_by(object)
        finally:
            police.release()

    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(hold_other)
        reader = None
        try:
            assert other_held.wait(5)
            reader = executor.submit(find)
            police.queued.get(timeout=5)
            if forget_wanted:
                police.forget(marker)
            return_other.set()
            holder.result(5)
            if not forget_wanted:
                police.queued.get(timeout=5)
                police.release()
            assert reader.result(5) is (None if forget_wanted else wanted)
        finally:
            return_other.set()
            police.release()
            if reader is not None:
                reader.result(5)
    assert not police._signals and not police._cursor
    assert police.rsize() == police.qsize() == 2
    police.kill(wanted)
    police.kill(other)


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
    traffic = AsyncTraffic()
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
    traffic = AsyncTraffic(TrafficState.USED)
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


def test_zero_capacity_get_fails_without_registering_object() -> None:
    police: TrafficPolice[Any] = TrafficPolice(maxsize=0)
    with pytest.raises(UnavailableTraffic):
        police.get_nowait()
    assert not police.busy
    assert police.rsize() == police.qsize() == 0


@pytest.mark.asyncio
async def test_async_signal_contracts_and_priority() -> None:
    police = AsyncTrafficPolice[Any]()
    signals = police._signals
    traffic = AsyncTraffic(TrafficState.USED)
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
    traffic = AsyncTraffic(TrafficState.USED)
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
    traffic = AsyncTraffic(TrafficState.SATURATED)
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


def test_put_rejects_another_threads_checkout() -> None:
    police: TrafficPolice[Any] = TrafficPolice()
    first, second = Traffic(), Traffic()
    a: Any = object()
    b: Any = object()
    police.put(second, b)
    police.put(first, a, immediately_unavailable=True)
    held, finish = Event(), Event()

    def owner() -> None:
        police.locate(b)
        held.set()
        try:
            assert finish.wait(10)
        finally:
            police.release()

    with ThreadPoolExecutor(max_workers=1) as executor:
        worker = executor.submit(owner)
        try:
            assert held.wait(5)
            with pytest.raises(AtomicTraffic):
                police.put(second, immediately_unavailable=True)
            assert police.is_held(first)
            police.release()
            with pytest.raises(UnavailableTraffic):
                police.locate(b, block=False)
        finally:
            finish.set()
            worker.result(5)
    police.clear()
    assert first.close_count == second.close_count == 1


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
async def test_type_borrow_rejects_a_different_existing_owner(
    asynchronous: bool,
) -> None:
    police = make_police(asynchronous)
    first, second = make_traffic(asynchronous), make_traffic(asynchronous)
    await store(police, first, object())
    await store(police, second, ("other",))
    assert await call(police.get_nowait) is second
    with pytest.raises(AtomicTraffic):
        if asynchronous:
            async with police.borrow(object):
                pytest.fail("another object is already held")
        else:
            with police.borrow(object):
                pytest.fail("another object is already held")
    police.release()
    assert police.rsize() == police.qsize() == 2
    await call(police.clear)
    assert first.close_count == second.close_count == 1


@pytest.mark.asyncio
async def test_failed_pool_close_does_not_allow_reacquiring_retired_owner(
    asynchronous: bool,
) -> None:
    parent = make_police(asynchronous, concurrency=True)
    child = make_police(asynchronous)
    owner = make_traffic(asynchronous)
    owner.pool = child
    owner.fail_close = True
    marker = ResponsePromise(owner, 1, [])
    await store(parent, owner, marker)
    await call(parent.clear)
    assert owner.close_count == 1 and parent.rsize() == 0
    with pytest.raises(UnavailableTraffic):
        await call(parent.locate, marker, block=False)
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
    traffic = AsyncTraffic()
    marker: Any = object()
    await store(child, traffic, marker)
    child.forget(marker)
    assert not child._map
    assert child.qsize() == 1
    await child.clear()


@pytest.mark.asyncio
async def test_async_specific_waiter_wakes_when_owner_finishes() -> None:
    police = ObservedAsyncPolice()
    traffic = AsyncTraffic(TrafficState.USED)
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
