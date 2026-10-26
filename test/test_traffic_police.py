from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event, Thread, current_thread
from typing import Any

import pytest

from test.traffic_police_helpers import Connection, ObservedPolice, Traffic
from urllib3 import HTTPResponse
from urllib3.backend import ResponsePromise
from urllib3.util.traffic_police import (
    AtomicTraffic,
    OverwhelmedTraffic,
    TrafficPolice,
    TrafficState,
    UnavailableTraffic,
)


def store(police: Any, traffic: Any, *markers: Any) -> None:
    police.put(traffic, *markers, immediately_unavailable=True)
    police.release()


def test_memorize_during_borrow_does_not_reserve_checkout() -> None:
    police: TrafficPolice[Any] = TrafficPolice()
    conn: Any = Connection()
    marker = HTTPResponse()
    police.put(conn, marker)
    with police.borrow(marker):
        response = HTTPResponse(connection=conn, police_officer=police)
        assert police.beacon(response)
        assert police._reclaim_reservation(response) is None
        assert police.is_held(conn)


def test_reserving_forgotten_response_does_not_restore_route() -> None:
    police: TrafficPolice[Any] = TrafficPolice()
    conn, response = Connection(), HTTPResponse()
    police.put(conn, response, immediately_unavailable=True)
    police.forget(response)
    police.memorize(response, conn, reserve=True)
    assert not police.beacon(response)
    assert police._reclaim_reservation(response) is None
    police.release()


@pytest.mark.parametrize("unrelated_checkout", [False, True])
def test_reclaim_response_reservation(unrelated_checkout: bool) -> None:
    police: TrafficPolice[Any] = TrafficPolice()
    conn, other, response = Connection(), Connection(), HTTPResponse()

    def reserve() -> None:
        police.put(conn, response, immediately_unavailable=True)
        police.memorize(response, conn, reserve=True)

    with ThreadPoolExecutor(1) as executor:
        executor.submit(reserve).result(5)
        if unrelated_checkout:
            police.put(other, immediately_unavailable=True)
        assert police._reclaim_reservation(response) is (
            None if unrelated_checkout else conn
        )
        assert police.is_held(other if unrelated_checkout else conn)
        assert not police._reclaim_reservation(response)
        police.release()
        executor.submit(police.release).result(5)
    assert not police._cursor


@pytest.mark.parametrize(
    "operation", ["borrow", "borrow_none", "locate", "release", "put"]
)
def test_intervening_operation_invalidates_reservation(operation: str) -> None:
    police: TrafficPolice[Any] = TrafficPolice()
    conn, response, neighbour = Connection(), HTTPResponse(), HTTPResponse()
    police.put(conn, response, neighbour, immediately_unavailable=True)
    police.memorize(response, conn, reserve=True)
    if operation in ("borrow", "borrow_none"):
        with police.borrow(neighbour if operation == "borrow" else None):
            police.memorize(
                neighbour, conn, reserve=True
            )  # An active nested borrow cannot be reserved.
            assert not police._reclaim_reservation(response)
            assert not police._reclaim_reservation(neighbour)
            # release_conn() can return ownership inside an active borrow.
            police.release()
    elif operation == "locate":
        police.locate(neighbour)
        police.release()
    elif operation == "put":
        police.put(conn)
    else:
        police.release()
    assert not police._reclaim_reservation(response)
    police.release()
    assert not police._cursor


def test_handoff_does_not_inherit_response_reservation() -> None:
    police = ObservedPolice()
    conn, response, neighbour = Connection(), HTTPResponse(), HTTPResponse()
    police.put(conn, response, neighbour, immediately_unavailable=True)
    police.memorize(response, conn, reserve=True)
    acquired, finish = Event(), Event()

    def read() -> None:
        with police.borrow(neighbour):
            acquired.set()
            assert finish.wait(5)
            assert police.is_held(conn)

    with ThreadPoolExecutor(1) as executor:
        reader = executor.submit(read)
        try:
            police.queued.get(timeout=5)
            police.release()
            assert acquired.wait(5)
            assert not police._reclaim_reservation(response)
        finally:
            finish.set()
        reader.result(5)
    assert not police._cursor


def test_kill_returned_target_preserves_unrelated_cursor() -> None:
    police: TrafficPolice[Any] = TrafficPolice()
    target, other = Connection(), Connection(saturated=True)
    police.put(target, immediately_unavailable=True)
    police.release()
    police.put(other, immediately_unavailable=True)
    assert police.kill(target, idle_only=True)
    assert target.closed and not other.closed
    assert police.is_held(other)
    assert id(target) not in police._registry and id(target) not in police._container
    assert not police.kill(target, idle_only=True)
    police.kill_cursor()
    assert other.closed and not police.busy
    assert police.rsize() == police.qsize() == 0


def test_kill_idle_waits_for_nested_borrows() -> None:
    police: TrafficPolice[Any] = TrafficPolice()
    target, indicator = Connection(), HTTPResponse()
    police.put(target, indicator, immediately_unavailable=True)
    police.release()
    with police.borrow(indicator):
        with police.borrow(indicator):
            assert not police.kill(target, idle_only=True)
        assert not police.kill(target, idle_only=True)
        assert not target.closed
    assert police.kill(target, idle_only=True)
    assert target.closed and not police._map


@pytest.mark.parametrize("concurrency", [False, True])
@pytest.mark.parametrize("wire_finished", [False, True])
def test_shutdown_waits_for_last_borrow(concurrency: bool, wire_finished: bool) -> None:
    police: TrafficPolice[Any] = TrafficPolice(concurrency=concurrency)
    target, indicator = Connection(saturated=True), HTTPResponse()
    police.put(target, indicator, immediately_unavailable=True)
    if wire_finished:
        setattr(target, "_pending_responses", {1: indicator})
    police.release()
    acquired, release = Event(), Event()
    finished: Future[None] = Future()

    def borrow() -> None:
        try:
            with police.borrow(indicator):
                with police.borrow(indicator):
                    acquired.set()
                    assert release.wait(5)
                assert not target.closed
            finished.set_result(None)
        except BaseException as exc:
            finished.set_exception(exc)

    worker = Thread(target=borrow)
    worker.start()
    try:
        assert acquired.wait(5)
        police.clear()
        target.is_saturated, target.is_idle = False, True
        if wire_finished:
            # The backend has received EOF, but the response still owns its borrow.
            setattr(target, "_pending_responses", {})
        police.clear()
        assert not target.closed
        release.set()
        finished.result(5)
        assert target.closed
        assert police.rsize() == police.qsize() == 0
        assert not police._shutdown
    finally:
        release.set()
        worker.join(5)
        assert not worker.is_alive()


def test_shutdown_disposes_idle_target_after_borrow_error() -> None:
    police: TrafficPolice[Any] = TrafficPolice()
    target, indicator = Connection(saturated=True), HTTPResponse()
    police.put(target, indicator, immediately_unavailable=True)
    police.release()
    with pytest.raises(ValueError, match="response processing failed"):
        with police.borrow(indicator):
            police.clear()
            target.is_saturated, target.is_idle = False, True
            raise ValueError("response processing failed")
    assert target.closed
    assert police.rsize() == police.qsize() == 0


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


@pytest.mark.parametrize("operation", ["locate", "locate_none", "factory", "type"])
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
            if operation == "type":
                with police.borrow(HTTPResponse) as found:
                    result.set_result(found)
            elif operation == "factory":
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
                assert police._signals[0].target_conn_or_pool is (
                    None if operation == "type" else conn
                )
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


@pytest.mark.parametrize("concurrency", [False, True])
def test_type_borrow_and_beacon_release_ownership(concurrency: bool) -> None:
    police: TrafficPolice[Any] = TrafficPolice(maxsize=1, concurrency=concurrency)
    conn, indicator = Connection(saturated=True), HTTPResponse()
    police.put(conn, indicator)

    assert police.beacon(HTTPResponse)
    assert not police.busy
    assert police.qsize() == 1
    with police.borrow(HTTPResponse) as found:
        assert found is conn
        assert police.is_held(conn)
        assert police.qsize() == int(concurrency)
        with police.borrow() as nested:
            assert nested is conn
        assert police.is_held(conn)
    assert not police._cursor
    assert not police._signals
    assert police.qsize() == 1


@pytest.mark.parametrize("concurrency", [False, True])
def test_type_beacon_preserves_existing_owner(concurrency: bool) -> None:
    police: TrafficPolice[Any] = TrafficPolice(maxsize=2, concurrency=concurrency)
    conn, other = Connection(saturated=True), Connection(saturated=True)
    indicator, other_indicator = HTTPResponse(), HTTPResponse()
    police.put(conn, indicator)
    police.put(other, other_indicator)

    with police.borrow(other_indicator):
        assert police.beacon(HTTPResponse)
        assert police.is_held(other)
        assert not police.is_held(conn)
        assert police.qsize() == 1 + int(concurrency)
    assert not police._cursor
    assert police.qsize() == 2


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


def test_zero_sized_pool_reservation_preserves_legacy_behavior() -> None:
    police: TrafficPolice[Any] = TrafficPolice(maxsize=0)
    conn = Connection()
    with police.locate_or_hold(HTTPResponse(), block=False) as swap:
        swap(conn)
    assert police.is_held(conn)
    police.release()


@pytest.mark.parametrize("state", list(TrafficState))
@pytest.mark.parametrize("concurrency", [False, True])
def test_state_views_and_selection(state: TrafficState, concurrency: bool) -> None:
    police: Any = TrafficPolice(concurrency=concurrency)
    traffic: Any = Traffic(state)
    assert police.rsize() == police.qsize() == 0
    assert not police.bag_only_saturated
    store(police, traffic)
    assert police.bag_only_idle is (state is TrafficState.IDLE)
    assert police.bag_only_saturated is (state is TrafficState.SATURATED)
    assert state.name.title() in repr(police)
    assert police.get_nowait() is traffic
    assert police.is_held(traffic)
    assert police.qsize() == int(concurrency)
    police.release()
    assert not police.busy
    assert police.qsize() == 1
    traffic.state = TrafficState.IDLE
    police.clear()
    assert traffic.close_count == 1


@pytest.mark.parametrize("filter_name", ["non_saturated_only", "not_idle_only"])
@pytest.mark.parametrize("concurrency", [False, True])
def test_selection_filters_skip_ineligible_objects(
    filter_name: str, concurrency: bool
) -> None:
    police: Any = TrafficPolice(concurrency=concurrency)
    rejected_state = (
        TrafficState.SATURATED
        if filter_name == "non_saturated_only"
        else TrafficState.IDLE
    )
    rejected: Any = Traffic(rejected_state)
    accepted: Any = Traffic(TrafficState.USED)
    store(police, rejected)
    store(police, accepted)
    assert police.get_nowait(**{filter_name: True}) is accepted
    police.release()
    police.kill(accepted)
    with pytest.raises(UnavailableTraffic):
        police.get_nowait(**{filter_name: True})
    assert not police.busy
    assert police.rsize() == police.qsize() == 1
    police.kill(rejected)


def test_argument_and_ownership_errors_preserve_current_checkout() -> None:
    police: Any = TrafficPolice()
    first, second = Traffic(), Traffic()
    a, b = object(), object()
    store(police, first, a)
    store(police, second, b)
    with pytest.raises(AtomicTraffic):
        police.memorize(object())
    with pytest.raises(ValueError, match="unsupported traffic_indicator"):
        police.locate(object)
    assert police.locate(a) is first
    with pytest.raises(AtomicTraffic):
        police.get_nowait()
    with pytest.raises(AtomicTraffic):
        police.locate(b)
    with pytest.raises(AtomicTraffic):
        list(police.iter_idle())
    assert police.is_held(first)
    police.release()
    assert police.locate(b) is second
    police.release()
    police.clear()
    assert first.close_count == second.close_count == 1


@pytest.mark.parametrize("concurrency", [False, True])
def test_duplicate_put_is_idempotent(concurrency: bool) -> None:
    police: Any = TrafficPolice(concurrency=concurrency)
    traffic: Any = Traffic()
    store(police, traffic)
    police.put(traffic)
    assert police.rsize() == police.qsize() == 1
    assert not police.busy
    police.clear()
    assert traffic.close_count == 1


@pytest.mark.parametrize("held", [False, True])
def test_shutdown_put_disposes_finished_object(held: bool) -> None:
    police: Any = TrafficPolice()
    traffic: Any = Traffic(TrafficState.USED)
    indicator = object()
    police.put(traffic, indicator, immediately_unavailable=True)
    police.clear()
    assert police._shutdown and traffic.close_count == 0
    if not held:
        police.release()
    traffic.state = TrafficState.IDLE
    police.put(traffic)
    assert traffic.close_count == 1
    assert not police.busy and not police._map
    assert police.rsize() == police.qsize() == 0


@pytest.mark.parametrize("fail_close", [False, True])
def test_capacity_eviction_cleans_routes_even_if_close_fails(fail_close: bool) -> None:
    police: Any = TrafficPolice(maxsize=1)
    old, new = Traffic(), Traffic()
    old.fail_close = fail_close
    marker = object()
    store(police, old, marker)
    police.put(new, block=False, immediately_unavailable=True)
    police.release()
    assert old.close_count == 1 and new.close_count == 0
    assert not police.beacon(marker)
    assert police.rsize() == police.qsize() == 1
    assert police.get_nowait() is new
    police.release()
    police.clear()


def test_capacity_refuses_discarding_used_object() -> None:
    police: Any = TrafficPolice(maxsize=1)
    old: Any = Traffic(TrafficState.USED)
    new: Any = Traffic()
    store(police, old)
    with pytest.raises(OverwhelmedTraffic):
        police.put(new, block=False)
    assert police.rsize() == police.qsize() == 1
    assert old.close_count == new.close_count == 0
    police.kill(old)
    police._sacrifice_first_idle(block=False)


@pytest.mark.parametrize("concurrency", [False, True])
def test_nested_borrow_without_indicator(concurrency: bool) -> None:
    police: Any = TrafficPolice(concurrency=concurrency)
    traffic: Any = Traffic()
    store(police, traffic)
    with police.borrow() as outer:
        with police.borrow() as inner:
            assert outer is inner is traffic
        assert police.is_held(traffic)
    assert not police.busy and police.qsize() == 1
    police.clear()


def test_empty_borrow_returns_its_reservation() -> None:
    police: Any = TrafficPolice(maxsize=1)
    with pytest.raises(UnavailableTraffic):
        with police.borrow(block=False):
            pytest.fail("no object is registered")
    assert not police.busy
    police.clear()


def test_beacon_finds_indicator_in_child_police() -> None:
    parent: Any = TrafficPolice(concurrency=True)
    child: Any = TrafficPolice()
    owner: Any = Traffic()
    connection = Traffic()
    owner.pool = child
    child.parent = parent
    marker = object()
    store(child, connection, marker)
    store(parent, owner)
    assert parent.locate(marker) is owner
    parent.release()
    assert child.beacon(marker)
    child.forget(marker)
    assert not parent.locate(marker)
    child.clear()
    parent.clear()


def test_tuple_indicator_and_readiness_rejection() -> None:
    police: Any = TrafficPolice()
    traffic: Any = Traffic(TrafficState.USED)
    marker = ("host", 443)
    store(police, traffic, marker)
    assert police.beacon(marker)
    assert police.beacon(tuple)
    assert not police.busy
    with pytest.raises(UnavailableTraffic, match="Candidate is not ready"):
        police.locate(
            marker,
            block=False,
            conn_pre_pick_callable=lambda _: lambda: None,
        )
    assert not police.busy and police.qsize() == 1
    assert police.locate(marker) is traffic
    police.release()
    police.forget(marker)
    assert not police.beacon(marker)
    police.kill(traffic)


def test_shared_police_rejects_readiness_callback() -> None:
    police: Any = TrafficPolice(concurrency=True)
    with pytest.raises(ValueError, match="exclusive ownership"):
        police.locate(object(), conn_pre_pick_callable=lambda _: None)
    assert not police.busy


@pytest.mark.parametrize("concurrency", [False, True])
def test_idle_iterator_skips_disconnected_objects(concurrency: bool) -> None:
    police: Any = TrafficPolice(concurrency=concurrency)
    dead, live = Traffic(), Traffic()
    dead.is_connected = False
    store(police, dead)
    store(police, live)
    selected = list(police.iter_idle())
    assert selected == [live]
    assert dead.close_count == 1 and live.close_count == 0
    assert not police.busy
    police.clear()


def test_abandoned_reservation_is_reclaimed() -> None:
    police: Any = TrafficPolice(maxsize=1)
    marker = ("host", 443)
    assert not police.busy_with_placeholder
    with police.locate_or_hold(marker):
        assert police.busy_with_placeholder
    assert not police.busy and police.rsize() == 0
    assert not police.beacon(marker)


def test_empty_tracks_registered_objects() -> None:
    police: Any = TrafficPolice()
    assert police.empty()
    traffic: Any = Traffic()
    store(police, traffic)
    assert not police.empty()
    police.clear()
    assert police.empty()


def test_new_put_makes_object_available() -> None:
    police: Any = TrafficPolice()
    traffic: Any = Traffic()
    marker = object()
    police.put(traffic, marker)
    assert police.rsize() == police.qsize() == 1
    assert police.get_nowait() is traffic
    police.release()
    assert police.beacon(marker)
    police.clear()
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


def test_zero_capacity_get_fails_without_registering_object() -> None:
    police: TrafficPolice[Any] = TrafficPolice(maxsize=0)
    with pytest.raises(UnavailableTraffic):
        police.get_nowait()
    assert not police.busy
    assert police.rsize() == police.qsize() == 0


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


def test_type_borrow_rejects_a_different_existing_owner() -> None:
    police: Any = TrafficPolice()
    first, second = Traffic(), Traffic()
    store(police, first, object())
    store(police, second, ("other",))
    assert police.get_nowait() is second
    with pytest.raises(AtomicTraffic):
        with police.borrow(object):
            pytest.fail("another object is already held")
    police.release()
    assert police.rsize() == police.qsize() == 2
    police.clear()
    assert first.close_count == second.close_count == 1


def test_failed_pool_close_does_not_allow_reacquiring_retired_owner() -> None:
    parent: Any = TrafficPolice(concurrency=True)
    child: Any = TrafficPolice()
    owner: Any = Traffic()
    owner.pool = child
    owner.fail_close = True
    marker = ResponsePromise(owner, 1, [])
    store(parent, owner, marker)
    parent.clear()
    assert owner.close_count == 1 and parent.rsize() == 0
    with pytest.raises(UnavailableTraffic):
        parent.locate(marker, block=False)
    parent.forget(marker)
    assert not parent._map and not parent.busy
