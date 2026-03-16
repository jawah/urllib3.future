from __future__ import annotations

import asyncio
import collections
import contextlib
import sys
import time
import typing
from dataclasses import dataclass, field

from ..traffic_police import (
    AtomicTraffic,
    OverwhelmedTraffic,
    TrafficState,
    UnavailableTraffic,
    traffic_state_of,
    ItemPlaceholder,
)
from ...contrib.ssa._timeout import timeout as ctx_expire_in

if typing.TYPE_CHECKING:
    from ..._async.connection import AsyncHTTPConnection
    from ..._async.connectionpool import AsyncConnectionPool
    from ..._async.response import AsyncHTTPResponse
    from ...backend import ResponsePromise
    from ...poolmanager import PoolKey

    MappableTraffic: typing.TypeAlias = typing.Union[
        AsyncHTTPResponse, ResponsePromise, PoolKey
    ]
    ManageableTraffic: typing.TypeAlias = typing.Union[
        AsyncHTTPConnection, AsyncConnectionPool
    ]

    T = typing.TypeVar("T", bound=ManageableTraffic)
else:
    T = typing.TypeVar("T")


@dataclass
class ActiveCursor(typing.Generic[T]):
    """Duplicated from sync part for typing reasons. 'T' in TrafficPolice sync is bound to sync types!"""

    obj_id: int
    conn_or_pool: T
    depth: int = 1


def _current_task_or_die() -> asyncio.Task[typing.Any]:
    t = asyncio.current_task()
    if t is None:
        raise RuntimeError(
            "Attempt to call function outside of (async) running event loop"
        )
    return t


@dataclass
class AsyncPendingSignal(typing.Generic[T]):
    """A task may want to block on a specific signal to avoid consuming CPU needlessly."""

    owner_task: asyncio.Task[typing.Any] = field(default_factory=_current_task_or_die)
    #: the signal itself, when the condition is met, it's set.
    event: asyncio.Event = field(default_factory=asyncio.Event)

    #: the target makes the signal only trigger for that particular conn_or_pool
    target_conn_or_pool: T | None = None
    target_obj_id: int | None = None

    #: what conn_or_pool you get after the signal is received (if it's there, it's safe to use)
    conn_or_pool: T | None = None

    #: limited suite of state we want for the conn_or_pool or None
    states: tuple[TrafficState, ...] = (
        TrafficState.IDLE,
        TrafficState.USED,
        TrafficState.SATURATED,
    )


# Target delay in seconds (100 µs). This is the approximate time we want
# the event-loop thread to stall so the kernel can accumulate
# several incoming segments before the next recv() call.
_COALESCE_DELAY: float = 0.0001

if sys.platform == "win32":
    # Windows lacks nanosleep, let's improvise!
    # A perf_counter spin-wait is the only portable way to get consistent
    # sub-millisecond blocking on Windows. 100 µs of busy-waiting is
    # negligible in practice (< 0.5 % of a typical request lifecycle).
    _perf_counter = time.perf_counter

    def _kernel_recv_coalesce() -> None:
        deadline = _perf_counter() + _COALESCE_DELAY
        while _perf_counter() < deadline:
            pass

else:
    # Every POSIX platform (Linux, macOS, FreeBSD, ...) exposes nanosleep
    # through time.sleep() with µs-level accuracy.  This is both precise
    # and CPU-friendly: the thread truly sleeps instead of spinning.
    # we are effectively letting the kernel coalesce as much packet as possible.
    def _kernel_recv_coalesce() -> None:
        time.sleep(_COALESCE_DELAY)


def _intent_to_write(*states: TrafficState) -> bool:
    """Infer whether the caller wants to WRITE (True) or READ (False)
    based on the traffic states being queried.

    Querying exclusively for states with remaining request capacity
    (IDLE, USED) implies a WRITE intent — looking for a connection to
    send a new request on.
    Querying for states with open streams that may be at capacity
    (SATURATED) implies a READ intent — waiting to consume responses.
    """
    return all(state < TrafficState.SATURATED for state in states)


class AsyncSignals(typing.Generic[T]):
    """The signal mgr counterpart of our synchronous implementation but for monothreaded/goto label version."""

    def __init__(
        self, cursors: dict[asyncio.Task[typing.Any], ActiveCursor[T]]
    ) -> None:
        self._priority_signals: collections.deque[AsyncPendingSignal[T]] = (
            collections.deque()
        )
        self._furthest_signals: collections.deque[AsyncPendingSignal[T]] = (
            collections.deque()
        )

        #: set if there is no waiter for write + no specific target
        self._generic_waiters: asyncio.Event = asyncio.Event()
        self._generic_waiters.set()

        self._saturated_signals: dict[int, asyncio.Event] = {}

        #: ref to higher cursors held in AsyncTrafficPolice
        self._cursors: dict[asyncio.Task[typing.Any], ActiveCursor[T]] = cursors

        self._writing_tasks: set[asyncio.Task[typing.Any]] = set()

        self._last_dispatch_end = 0.0
        self._ema_gap = 0.0

    def declare_writing(self) -> None:
        self._writing_tasks.add(_current_task_or_die())

    def terminate_writing(self) -> bool:
        current_task = _current_task_or_die()
        if current_task in self._writing_tasks:
            self._writing_tasks.remove(current_task)
            return self._refresh_generic_waiters()
        return False

    def _refresh_generic_waiters(self) -> bool:
        if self._writing_tasks or self._priority_signals:
            if self._generic_waiters.is_set():
                self._generic_waiters.clear()
                return True
        else:
            if not self._generic_waiters.is_set():
                self._generic_waiters.set()
                return True

        return False

    def register(
        self,
        target_conn_or_pool: T | None,
        *states: TrafficState,
    ) -> AsyncPendingSignal[T]:
        signal = AsyncPendingSignal(
            states=states,
            target_conn_or_pool=target_conn_or_pool,
        )

        if target_conn_or_pool is not None:
            signal.target_obj_id = id(target_conn_or_pool)

        if target_conn_or_pool is not None:
            obj_id = id(target_conn_or_pool)

            if obj_id not in self._saturated_signals:
                self._saturated_signals[obj_id] = asyncio.Event()

        if _intent_to_write(*states):
            self._priority_signals.appendleft(signal)
        else:
            self._furthest_signals.append(signal)

        return signal

    def unregister(
        self,
        signal: AsyncPendingSignal[T],
        unblock: bool = False,
    ) -> None:
        if signal in self._priority_signals:
            self._priority_signals.remove(signal)
        else:
            self._furthest_signals.remove(signal)
        if unblock:
            signal.event.set()

    def should_queue_read_operation(
        self,
        target_conn_or_pool: T,
    ) -> bool:
        if self._generic_waiters.is_set():
            return False
        elif traffic_state_of(target_conn_or_pool) is TrafficState.SATURATED:
            return False

        obj_id = id(target_conn_or_pool)

        if obj_id not in self._saturated_signals:
            self._saturated_signals[obj_id] = asyncio.Event()

        if self._saturated_signals[obj_id].is_set():
            return False
        elif self._priority_signals:
            return True

        return False

    def kill(self) -> bool:
        current_task = _current_task_or_die()

        if current_task not in self._cursors:
            raise AtomicTraffic

        cursor = self._cursors[current_task]

        any_broadcast_push: bool = False

        if cursor.obj_id in self._saturated_signals:
            condition = self._saturated_signals[cursor.obj_id]
            condition.set()
            any_broadcast_push = True

        awoken_signals = []

        if self._priority_signals:
            awoken_signal = self._priority_signals.popleft()
            awoken_signal.event.set()
            any_broadcast_push = True

        for signal in self._furthest_signals:
            if signal.target_obj_id == cursor.obj_id:
                signal.event.set()
                awoken_signals.append(signal)

        for awoken_signal in awoken_signals:
            if awoken_signal in self._priority_signals:
                self._priority_signals.remove(awoken_signal)
            else:
                self._furthest_signals.remove(awoken_signal)

        del self._cursors[current_task]

        if awoken_signals:
            return True

        return any_broadcast_push

    def next(self) -> bool:
        current_task = _current_task_or_die()

        if current_task not in self._cursors:
            raise AtomicTraffic

        cursor = self._cursors[current_task]
        current_state = traffic_state_of(cursor.conn_or_pool)

        # this code will certainly surprise most people
        # in our investigations, the signal management
        # was often "too fast" to let the Kernel stack
        # into the buffer coalesced TCP packets, leading
        # the event loop to wasting a couple of iterations
        # (e.g. epoll_wait) when a single one was sufficient
        # and asyncio/OSes don't have a "wake me when
        # we have more than x bytes".
        # so we were creative (or foolish) enough to
        # stall the event loop in highly concurrent scenarii
        # where we expect plenty of bytes into the socket.
        # bellow is heuristic, caution. it's not a silver
        # bullet.
        if not getattr(cursor.conn_or_pool, "_fast_recv_mode", True):
            now = time.perf_counter()

            # Natural gap = time the event loop spent between dispatches
            # (excludes our own sleep from last time)
            if self._last_dispatch_end > 0:
                natural_gap = now - self._last_dispatch_end
                # Smooth with EMA to avoid reacting to transient spikes
                self._ema_gap = self._ema_gap * 0.7 + natural_gap * 0.3

                remaining = _COALESCE_DELAY - self._ema_gap

                if remaining > 0:
                    _kernel_recv_coalesce()  # or spin-wait on Windows

            # ... bookkeeping, signal dispatch ...
            self._last_dispatch_end = time.perf_counter()

        can_write_more: bool = current_state is not TrafficState.SATURATED
        can_read_anything: bool = current_state is not TrafficState.IDLE
        can_only_write: bool = current_state is TrafficState.IDLE

        any_broadcast_push: bool = False

        if cursor.obj_id in self._saturated_signals:
            event = self._saturated_signals[cursor.obj_id]

            if current_state is TrafficState.SATURATED:
                if not event.is_set():
                    event.set()
                    any_broadcast_push = True
            else:
                if event.is_set():
                    event.clear()

        anything_pending_write: bool = bool(self._priority_signals)

        if anything_pending_write and can_write_more:
            early_unpack_signal = self._priority_signals.pop()

            early_unpack_signal.conn_or_pool = cursor.conn_or_pool

            # transfer the cursor
            del self._cursors[current_task]
            self._cursors[early_unpack_signal.owner_task] = cursor
            cursor.depth = 1

            # mark task as currently in writing
            if current_task in self._writing_tasks:
                self._writing_tasks.remove(current_task)

            self._writing_tasks.add(early_unpack_signal.owner_task)

            early_unpack_signal.event.set()
            self._refresh_generic_waiters()
            return True

        if (can_only_write and not anything_pending_write) or not can_read_anything:
            return any_broadcast_push

        late_signal_elected: AsyncPendingSignal[T] | None = None

        for signal in self._furthest_signals:
            if current_state not in signal.states:
                continue

            if signal.target_obj_id == cursor.obj_id:
                late_signal_elected = signal
                break

        if late_signal_elected is not None:
            late_signal_elected.conn_or_pool = cursor.conn_or_pool
            del self._cursors[current_task]
            self._cursors[late_signal_elected.owner_task] = cursor
            cursor.depth = 1

            late_signal_elected.event.set()

            self._furthest_signals.remove(late_signal_elected)

            self._refresh_generic_waiters()
            return True

        return any_broadcast_push


class AsyncTrafficPolice(typing.Generic[T]):
    """Task-safe extended-Queue implementation.

    This class is made to enforce the 'I will have order!' psychopath philosophy.
    Rational: Recent HTTP protocols handle concurrent streams, therefore it is
    not as flat as before, we need to answer the following problems:

    1) we cannot just dispose of the oldest connection/pool, it may have a pending response in it.
    2) we need a map, a dumb and simple GPS, to avoid wasting CPU resources searching for a response (promise resolution).
       - that would permit doing read(x) on one response and then read(x) on another without compromising the concerned
         connection.
       - multiplexed protocols can permit temporary locking of a single connection even if response isn't consumed
         (instead of locking through entire lifecycle single request).

    This program is (very) complex and need, for patches, at least both unit tests and integration tests passing.
    """

    def __init__(
        self,
        maxsize: int | None = None,
        concurrency: bool = False,
        strict_maxsize: bool = True,
    ):
        """
        :param maxsize: Maximum number of items that can be contained.
        :param concurrency: Whether to allow a single item to be used across multiple tasks.
        :param strict_maxsize: If True the scheduler does not increase maxsize for a temporary increase.
        """
        self.maxsize = maxsize
        self.concurrency = concurrency
        self.strict_maxsize = strict_maxsize

        self._original_maxsize = maxsize

        self._registry: dict[int, T] = {}
        self._container: dict[int, T] = {}

        self._map: dict[int | PoolKey, T] = {}
        self._map_types: dict[int | PoolKey, type] = {}

        self._shutdown: bool = False

        self._cursors: dict[asyncio.Task[typing.Any], ActiveCursor[T]] = {}

        self.parent: AsyncTrafficPolice | None = None  # type: ignore[type-arg]

        self._signals = AsyncSignals(self._cursors)

    @property
    def _cursor(self) -> ActiveCursor[T] | None:
        current_task = _current_task_or_die()

        if current_task in self._cursors:
            return self._cursors[current_task]

        return None

    def _unset_cursor(self) -> None:
        current_task = _current_task_or_die()

        if current_task in self._cursors:
            del self._cursors[current_task]

    @property
    def busy(self) -> bool:
        return self._cursor is not None

    def is_held(self, conn_or_pool: T) -> bool:
        active_cursor = self._cursor

        if active_cursor is None:
            return False

        return active_cursor.obj_id == id(conn_or_pool)

    @property
    def bag_only_idle(self) -> bool:
        return all(
            traffic_state_of(_) is TrafficState.IDLE for _ in self._registry.values()
        )

    @property
    def bag_only_saturated(self) -> bool:
        """All manageable traffic is saturated. No more capacity available."""
        if not self._registry:
            return False
        return all(
            traffic_state_of(_) is TrafficState.SATURATED
            for _ in self._registry.values()
        )

    @property
    def bag_not_saturated(self) -> bool:
        if not self._registry:
            return False

        for conn in self._registry.values():
            if traffic_state_of(conn) is not TrafficState.SATURATED:
                return True

        return False

    def __len__(self) -> int:
        return len(self._registry)

    def _map_clear(self, value: T) -> None:
        obj_id = id(value)

        if obj_id not in self._registry:
            return

        outdated_keys = []

        for key, val in self._map.items():
            if id(val) == obj_id:
                outdated_keys.append(key)

        for key in outdated_keys:
            del self._map[key]
            del self._map_types[key]

    async def _find_by(self, traffic_type: type, block: bool = True) -> T | None:
        """Find the first available conn or pool that is linked to at least one traffic type."""
        any_of: list[T] = []

        for k, v in self._map_types.items():
            if v is traffic_type:
                conn_or_pool = self._map[k]
                obj_id = id(conn_or_pool)

                if obj_id in self._container:
                    return conn_or_pool

                any_of.append(conn_or_pool)

        if not block:
            return None

        if any_of:
            signals = [
                self._signals.register(
                    conn_or_pool, TrafficState.USED, TrafficState.SATURATED
                )
                for conn_or_pool in any_of
            ]

            coros_to_signal: dict[asyncio.Task[typing.Any], AsyncPendingSignal[T]] = {
                asyncio.ensure_future(s.event.wait()): s for s in signals
            }
            futs = list(coros_to_signal.keys())

            try:
                done, pending = await asyncio.wait(
                    futs, return_when=asyncio.FIRST_COMPLETED
                )
            except asyncio.CancelledError:
                for task, signal in coros_to_signal.items():
                    self._signals.unregister(signal)
                    task.cancel()

                await asyncio.gather(*futs, return_exceptions=True)
            else:
                for task in pending:
                    task.cancel()

                first_coro = done.pop()
                target_signal = coros_to_signal[first_coro]

                for signal in signals:
                    if target_signal == signal:
                        continue

                    self._signals.unregister(signal)

                await asyncio.gather(*pending, return_exceptions=True)

                return target_signal.target_conn_or_pool

        return None

    async def kill_cursor(self) -> None:
        """In case there is no other way, a conn or pool may be unusable and should be destroyed.
        This make the scheduler forget about it."""
        active_cursor = self._cursor

        if active_cursor is None:
            return

        self._map_clear(active_cursor.conn_or_pool)

        del self._registry[active_cursor.obj_id]

        try:
            await active_cursor.conn_or_pool.close()
        except Exception:
            pass

        if not self.concurrency:
            self._signals.kill()
            await asyncio.sleep(0)
        else:
            del self._container[active_cursor.obj_id]
            self._unset_cursor()

    async def _sacrifice_first_idle(self, block: bool = True) -> None:
        """When trying to fill the bag, arriving at the maxsize, we may want to remove an item.
        This method try its best to find the most appropriate idle item and removes it.
        """
        eligible_obj_id, eligible_conn_or_pool = None, None

        if not self._registry:
            return

        for obj_id, conn_or_pool in self._registry.items():
            if (
                obj_id in self._container
                and traffic_state_of(conn_or_pool) is TrafficState.IDLE
            ):
                eligible_obj_id, eligible_conn_or_pool = obj_id, conn_or_pool
                break

        if eligible_obj_id is not None and eligible_conn_or_pool is not None:
            self._map_clear(eligible_conn_or_pool)

            del self._registry[eligible_obj_id]
            del self._container[eligible_obj_id]

            try:
                await eligible_conn_or_pool.close()
            except Exception:
                pass

            return

        if not block:
            raise OverwhelmedTraffic(
                "Cannot select a disposable connection to ease the charge. "
                "This usually means that your pool sizing is insufficient, "
                "please increase your pool maxsize appropriately. "
                f"Currently set at maxsize={self.maxsize}. Usually you "
                "want as much as the number of active tasks/connections."
            )

        signal = self._signals.register(
            None,
            TrafficState.IDLE,
        )

        try:
            await signal.event.wait()
        except asyncio.CancelledError:
            self._signals.unregister(signal)
            raise

        await self._sacrifice_first_idle(block=block)

    async def iter_idle(self) -> typing.AsyncGenerator[T, None]:
        """Iterate over idle conn contained in the container bag."""
        if self.busy:
            raise AtomicTraffic(
                "One connection/pool active per thread at a given time. "
                "Call release prior to calling this method."
            )

        current_task = _current_task_or_die()

        if self._container:
            idle_targets: list[tuple[int, T]] = []

            for cur_obj_id, cur_conn_or_pool in self._container.items():
                if traffic_state_of(cur_conn_or_pool) is not TrafficState.IDLE:
                    continue

                idle_targets.append((cur_obj_id, cur_conn_or_pool))

            for obj_id, conn_or_pool in idle_targets:
                if (
                    not self.concurrency
                ):  # i.e. no exclusive access to conn_or_pool, no lock.
                    del self._container[obj_id]

                if obj_id is not None and conn_or_pool is not None:
                    self._cursors[current_task] = ActiveCursor(obj_id, conn_or_pool)

                    if (
                        hasattr(conn_or_pool, "is_connected")
                        and not conn_or_pool.is_connected
                    ):
                        try:
                            await conn_or_pool.close()
                        except Exception:  # Defensive:
                            pass

                        self.release()

                        del self._container[obj_id]
                        del self._registry[obj_id]

                        continue

                    try:
                        yield conn_or_pool
                    finally:
                        if self.release():
                            await asyncio.sleep(0)

    async def put(
        self,
        conn_or_pool: T,
        *traffic_indicators: MappableTraffic,
        block: bool = True,
        immediately_unavailable: bool = False,
    ) -> None:
        # clear was called, each conn/pool that gets back must be destroyed appropriately.
        if self._shutdown:
            await self.kill_cursor()
            # Cleanup was completed, no need to act like this anymore.
            if not self._registry:
                self._shutdown = False
            return

        if (
            self.maxsize is not None
            and len(self._registry) >= self.maxsize
            and id(conn_or_pool) not in self._registry
        ):
            await self._sacrifice_first_idle(block=block)

        should_schedule_another_task: bool = False
        current_task = _current_task_or_die()
        active_cursor = self._cursor

        try:
            obj_id = id(conn_or_pool)
            registered_conn_or_pool = obj_id in self._registry

            if registered_conn_or_pool:
                if obj_id in self._container:
                    # calling twice put? for the same conn_or_pool[...]
                    # we remain conservative here on purpose. BC constraints with upstream.
                    return

                if active_cursor is not None:
                    if active_cursor.obj_id != obj_id:
                        raise AtomicTraffic(
                            "You must release the previous connection prior to this."
                        )

                    active_cursor.depth -= 1

                    if active_cursor.depth == 0:
                        if not self.concurrency:
                            if self._signals.terminate_writing():
                                should_schedule_another_task = True

                            if self._signals.next():
                                should_schedule_another_task = True
                            else:
                                self._unset_cursor()
                                self._container[obj_id] = conn_or_pool
                        else:
                            self._unset_cursor()
            else:
                self._registry[obj_id] = conn_or_pool

            if immediately_unavailable:
                # it's "registered" but not available to others. thus setting the cursor on it.
                self._cursors[current_task] = ActiveCursor(obj_id, conn_or_pool)

                is_placeholder = isinstance(conn_or_pool, ItemPlaceholder)

                if not is_placeholder and not self.concurrency:
                    self._signals.declare_writing()

                # even with immediately_unavailable, if we're on a non-exclusive bag, then it's shared.
                if self.concurrency is True and not is_placeholder:
                    self._container[obj_id] = conn_or_pool
                    should_schedule_another_task = True

            if traffic_indicators:
                for indicator in traffic_indicators:
                    self.memorize(indicator, conn_or_pool)
        finally:
            if should_schedule_another_task:
                await asyncio.sleep(0)

    async def get_nowait(
        self, non_saturated_only: bool = False, not_idle_only: bool = False
    ) -> T | None:
        return await self.get(
            block=False,
            non_saturated_only=non_saturated_only,
            not_idle_only=not_idle_only,
        )

    async def get(
        self,
        block: bool = True,
        timeout: float | None = None,
        non_saturated_only: bool = False,  # ie. wants WRITE
        not_idle_only: bool = False,  # ie. wants READ
    ) -> T | None:
        conn_or_pool = None

        if self._cursor is not None:
            raise AtomicTraffic(
                "One connection/pool active per task at a given time. "
                "Call release prior to calling this method."
            )

        # This part is ugly but set for backward compatibility
        # urllib3 used to fill the bag with 'None'. This simulates that
        # old and bad behavior.
        if (
            not self._container or self.bag_only_saturated
        ) and self.maxsize is not None:
            if self.maxsize > len(self._registry):
                await self.put(
                    ItemPlaceholder(),  # type: ignore[arg-type]
                    immediately_unavailable=True,
                    block=block,
                )
                return None

        current_task = _current_task_or_die()

        if self._container:
            if non_saturated_only:
                obj_id, conn_or_pool = None, None
                for cur_obj_id, cur_conn_or_pool in self._container.items():
                    if traffic_state_of(cur_conn_or_pool) is TrafficState.SATURATED:
                        continue
                    obj_id, conn_or_pool = cur_obj_id, cur_conn_or_pool
                    break
                if obj_id is not None:
                    del self._container[obj_id]
            else:
                if not not_idle_only:
                    obj_id, conn_or_pool = self._container.popitem()
                else:
                    obj_id, conn_or_pool = None, None
                    for cur_obj_id, cur_conn_or_pool in self._container.items():
                        if traffic_state_of(cur_conn_or_pool) is TrafficState.IDLE:
                            continue
                        obj_id, conn_or_pool = cur_obj_id, cur_conn_or_pool
                        break
                    if obj_id is not None:
                        del self._container[obj_id]

            if obj_id is not None and conn_or_pool is not None:
                self._cursors[current_task] = ActiveCursor(obj_id, conn_or_pool)

                if self.concurrency:
                    self._container[obj_id] = conn_or_pool
                else:
                    if non_saturated_only and not self.concurrency:
                        self._signals.declare_writing()

                return conn_or_pool

        if conn_or_pool is None:
            if block:
                if not_idle_only:
                    states: tuple[TrafficState, ...] = (
                        TrafficState.USED,
                        TrafficState.SATURATED,
                    )
                elif non_saturated_only:
                    states = (
                        TrafficState.USED,
                        TrafficState.IDLE,
                    )
                else:
                    states = (
                        TrafficState.IDLE,
                        TrafficState.USED,
                        TrafficState.SATURATED,
                    )

                signal = self._signals.register(
                    None,
                    *states,
                )

                try:
                    if timeout is not None:
                        try:
                            async with ctx_expire_in(delay=timeout):
                                await signal.event.wait()
                        except TimeoutError as e:
                            raise UnavailableTraffic(
                                f"No connection available within {timeout} second(s)"
                            ) from e
                    else:
                        await signal.event.wait()
                except asyncio.CancelledError:
                    self._signals.unregister(signal)
                    raise

                if (
                    signal.conn_or_pool is None
                ):  # case where a slot is free. (previously killed)
                    await self.put(
                        ItemPlaceholder(),  # type: ignore[arg-type]
                        immediately_unavailable=True,
                        block=block,
                    )
                    return None
                return signal.conn_or_pool

            raise UnavailableTraffic("No connection available")

        return conn_or_pool

    def memorize(
        self, traffic_indicator: MappableTraffic, conn_or_pool: T | None = None
    ) -> None:
        active_cursor = self._cursor

        if conn_or_pool is None and active_cursor is None:
            raise AtomicTraffic("No connection active on the current task")

        if conn_or_pool is None:
            assert active_cursor is not None
            obj_id, conn_or_pool = active_cursor.obj_id, active_cursor.conn_or_pool
        else:
            obj_id, conn_or_pool = id(conn_or_pool), conn_or_pool

            if obj_id not in self._registry:
                # we raised an exception before
                # after consideration, it's best just
                # to ignore!
                return

        if isinstance(traffic_indicator, tuple):
            self._map[traffic_indicator] = conn_or_pool
            self._map_types[traffic_indicator] = type(traffic_indicator)
        else:
            traffic_indicator_id = id(traffic_indicator)
            self._map[traffic_indicator_id] = conn_or_pool
            self._map_types[traffic_indicator_id] = type(traffic_indicator)

    def forget(self, traffic_indicator: MappableTraffic) -> None:
        key: PoolKey | int = (
            traffic_indicator
            if isinstance(traffic_indicator, tuple)
            else id(traffic_indicator)
        )

        if key not in self._map:
            return

        del self._map[key]
        del self._map_types[key]

        if self.parent is not None:
            try:
                self.parent.forget(traffic_indicator)
            except UnavailableTraffic:
                pass

    @contextlib.asynccontextmanager
    async def locate_or_hold(
        self,
        traffic_indicator: MappableTraffic | None = None,
        block: bool = True,
        placeholder_set: bool = False,
    ) -> typing.AsyncGenerator[typing.Callable[[T], typing.Awaitable[None]] | T]:
        """Reserve a spot into the TrafficPolice instance while you construct your conn_or_pool.

        Creating a conn_or_pool may or may not take significant time, in order
        to avoid having many thread racing for TrafficPolice insert, we must
        have a way to instantly reserve a spot meanwhile we built what
        is required.
        """
        if traffic_indicator is not None:
            conn_or_pool = await self.locate(
                traffic_indicator=traffic_indicator, block=block
            )

            if conn_or_pool is not None:
                yield conn_or_pool
                return

        traffic_indicators = []

        if traffic_indicator is not None:
            traffic_indicators.append(traffic_indicator)

        if not placeholder_set:
            await self.put(
                ItemPlaceholder(),  # type: ignore[arg-type]
                *traffic_indicators,
                immediately_unavailable=True,
                block=block,
            )

        swap_made: bool = False

        async def inner_swap(swappable_conn_or_pool: T) -> None:
            nonlocal swap_made

            swap_made = True

            active_cursor = self._cursor

            if (
                active_cursor is not None
            ):  # only allowed in tests, should never occur in real conditions.
                del self._registry[active_cursor.obj_id]
                self._unset_cursor()

            await self.put(
                swappable_conn_or_pool,
                *traffic_indicators,
                immediately_unavailable=True,
                block=False,
            )

        yield inner_swap

        if not swap_made:
            await self.kill_cursor()

    async def locate(
        self,
        traffic_indicator: MappableTraffic,
        block: bool = True,
        timeout: float | None = None,
    ) -> T | None:
        while True:
            if not isinstance(traffic_indicator, type):
                key: PoolKey | int = (
                    traffic_indicator
                    if isinstance(traffic_indicator, tuple)
                    else id(traffic_indicator)
                )

                if key not in self._map:
                    # we must fallback on beacon (sub police officer if any)
                    conn_or_pool, obj_id = None, None
                else:
                    conn_or_pool = self._map[key]
                    obj_id = id(conn_or_pool)
            else:
                raise ValueError("unsupported traffic_indicator")

            if (
                conn_or_pool is None
                and obj_id is None
                and not isinstance(traffic_indicator, tuple)
            ):
                for r_obj_id, r_conn_or_pool in self._registry.items():
                    if hasattr(r_conn_or_pool, "pool") and isinstance(
                        r_conn_or_pool.pool, AsyncTrafficPolice
                    ):
                        if await r_conn_or_pool.pool.beacon(traffic_indicator):
                            conn_or_pool, obj_id = r_conn_or_pool, r_obj_id
                            break

            if not isinstance(conn_or_pool, ItemPlaceholder):
                break

            await asyncio.sleep(0)

        if conn_or_pool is None or obj_id is None:
            return None

        active_cursor = self._cursor

        if active_cursor is not None:
            if active_cursor.obj_id == obj_id:
                active_cursor.depth += 1
                return active_cursor.conn_or_pool
            raise AtomicTraffic(
                "Seeking to locate a connection when having another one used, did you forget a call to release?"
            )

        if obj_id not in self._container or self._signals.should_queue_read_operation(
            conn_or_pool
        ):
            if not block:
                raise UnavailableTraffic("Unavailable connection")

            signal = self._signals.register(
                conn_or_pool,
                TrafficState.USED,
                TrafficState.SATURATED,
            )

            try:
                await signal.event.wait()
            except asyncio.CancelledError:
                self._signals.unregister(signal)
                raise
        else:
            self._cursors[_current_task_or_die()] = ActiveCursor(obj_id, conn_or_pool)

            if not self.concurrency:
                del self._container[obj_id]

        return conn_or_pool

    @contextlib.asynccontextmanager
    async def borrow(
        self,
        traffic_indicator: MappableTraffic | type | None = None,
        block: bool = True,
        timeout: float | None = None,
        not_idle_only: bool = False,
    ) -> typing.AsyncGenerator[T, None]:
        try:
            if traffic_indicator:
                if isinstance(traffic_indicator, type):
                    conn_or_pool = await self._find_by(traffic_indicator)

                    if conn_or_pool:
                        obj_id = id(conn_or_pool)
                        active_cursor = self._cursor

                        if active_cursor is not None:
                            if active_cursor.obj_id != obj_id:
                                raise AtomicTraffic(
                                    "Seeking to locate a connection when having another one used, did you forget a call to release?"
                                )
                            active_cursor.depth += 1
                        else:
                            self._cursors[_current_task_or_die()] = ActiveCursor(
                                obj_id, conn_or_pool
                            )

                            if not self.concurrency:
                                del self._container[obj_id]
                else:
                    conn_or_pool = await self.locate(
                        traffic_indicator, block=block, timeout=timeout
                    )
            else:
                # simulate reentrant lock/borrow
                # get_response PM -> get_response HPM -> read R
                if self._cursor is not None:
                    active_cursor = self._cursor
                    active_cursor.depth += 1
                    obj_id, conn_or_pool = (
                        active_cursor.obj_id,
                        active_cursor.conn_or_pool,
                    )
                else:
                    conn_or_pool = await self.get(
                        block=block, timeout=timeout, not_idle_only=not_idle_only
                    )
            if conn_or_pool is None:
                if traffic_indicator is not None:
                    raise UnavailableTraffic(
                        "No connection matches the traffic indicator (promise, response, ...)"
                    )
                raise UnavailableTraffic("No connection are available")
            yield conn_or_pool
        finally:
            if self.release():
                await asyncio.sleep(0)

    def release(self) -> bool:
        active_cursor = self._cursor
        cursor_transferred: bool = False
        if active_cursor is not None:
            active_cursor.depth -= 1

            if active_cursor.depth == 0:
                if not self.concurrency:
                    cursor_transferred = self._signals.next()
                    if not cursor_transferred:
                        self._container[active_cursor.obj_id] = (
                            active_cursor.conn_or_pool
                        )
                        self._unset_cursor()
                else:
                    self._unset_cursor()

        return cursor_transferred

    async def clear(self) -> None:
        """Shutdown traffic pool."""
        planned_removal = []

        for obj_id in self._container:
            if traffic_state_of(self._container[obj_id]) is TrafficState.IDLE:
                planned_removal.append(obj_id)

        for obj_id in planned_removal:
            del self._container[obj_id]

        # if we can't shut down them all, we need to toggle the shutdown bit to collect and close remaining connections.
        if len(self._registry) > len(planned_removal):
            self._shutdown = True

        for obj_id in planned_removal:
            conn_or_pool = self._registry.pop(obj_id)

            try:
                await conn_or_pool.close()
            except Exception:  # Defensive: we are in a force shutdown loop, we shall dismiss errors here.
                pass

            self._map_clear(conn_or_pool)

        active_cursor = self._cursor

        if active_cursor is not None:
            if active_cursor.obj_id in planned_removal:
                self._unset_cursor()

    def qsize(self) -> int:
        return len(self._container)

    def rsize(self) -> int:
        return len(self._registry)

    async def beacon(self, traffic_indicator: MappableTraffic | type) -> bool:
        if not isinstance(traffic_indicator, type):
            key: PoolKey | int = (
                traffic_indicator
                if isinstance(traffic_indicator, tuple)
                else id(traffic_indicator)
            )
            return key in self._map
        return await self._find_by(traffic_indicator) is not None

    def __repr__(self) -> str:
        is_saturated = self.bag_only_saturated
        is_idle = not is_saturated and self.bag_only_idle

        status: str

        if is_saturated:
            status = "Saturated"
        elif is_idle:
            status = "Idle"
        else:
            status = "Used"

        return f"<AsyncTrafficPolice {self.rsize()}/{self.maxsize} ({status})>"
