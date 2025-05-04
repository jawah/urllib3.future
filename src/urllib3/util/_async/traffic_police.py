from __future__ import annotations

import asyncio
import contextlib
import contextvars
import typing
from dataclasses import dataclass

from ..traffic_police import (
    AtomicTraffic,
    OverwhelmedTraffic,
    TrafficState,
    UnavailableTraffic,
    traffic_state_of,
    ItemPlaceholder,
)

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

    def __init__(self, maxsize: int | None = None, concurrency: bool = False):
        """
        :param maxsize: Maximum number of items that can be contained.
        :param concurrency: Whether to allow a single item to be used across multiple threads.
        """
        self.maxsize = maxsize
        self.concurrency = concurrency

        self._registry: dict[int, T] = {}
        self._container: dict[int, T] = {}

        self._map: dict[int | PoolKey, T] = {}
        self._map_types: dict[int | PoolKey, type] = {}

        self._shutdown: bool = False

        self.__ctx_cursor: contextvars.ContextVar[ActiveCursor[T] | None] = (
            contextvars.ContextVar("cursor", default=None)
        )

        self.parent: AsyncTrafficPolice | None = None  # type: ignore[type-arg]

    @property
    def _cursor(self) -> ActiveCursor[T] | None:
        try:
            return self.__ctx_cursor.get()
        except LookupError:
            return None

    def _set_cursor(self, value: ActiveCursor[T] | None) -> None:
        self.__ctx_cursor.set(value)

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

    async def wait_for_unallocated_or_available_slot(
        self, timeout: float | None = None
    ) -> None:
        """Wait for EITHER free slot in the pool OR one conn is not saturated!"""
        combined_wait: float = 0.0

        while True:
            if self.maxsize is None:  # case Inf.
                return

            if len(self._registry) < self.maxsize:
                return

            for obj_id, conn_or_pool in self._registry.items():
                if (
                    traffic_state_of(conn_or_pool) is not TrafficState.SATURATED  # type: ignore[arg-type]
                    and obj_id in self._container
                ):
                    return

            before = asyncio.get_running_loop().time()
            await asyncio.sleep(0.001)
            combined_wait += asyncio.get_running_loop().time() - before

            if timeout is not None and combined_wait >= combined_wait:
                raise TimeoutError(
                    "Timed out while waiting for conn_or_pool to become available"
                )

    async def wait_for_idle_or_available_slot(
        self, timeout: float | None = None
    ) -> None:
        combined_wait: float = 0.0

        while True:
            if self.maxsize is None:  # case Inf.
                return

            if len(self._registry) < self.maxsize:
                return

            for obj_id, conn_or_pool in self._registry.items():
                if (
                    traffic_state_of(conn_or_pool) is TrafficState.IDLE  # type: ignore[arg-type]
                    and obj_id in self._container
                ):
                    return

            before = asyncio.get_running_loop().time()
            await asyncio.sleep(0.001)
            combined_wait += asyncio.get_running_loop().time() - before

            if timeout is not None and combined_wait >= combined_wait:
                raise TimeoutError(
                    "Timed out while waiting for conn_or_pool to become available"
                )

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
        while True:
            any_match = False

            for k, v in self._map_types.items():
                if v is traffic_type:
                    conn_or_pool = self._map[k]
                    # this method may be subject to quick states mutation
                    # due to the (internal) map independent lock
                    if conn_or_pool.is_idle is True:
                        continue

                    any_match = True

                    obj_id = id(conn_or_pool)

                    if obj_id in self._container:
                        return conn_or_pool

            if block is False:
                break

            if any_match:
                await asyncio.sleep(0.001)
                continue

            break

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

        self._set_cursor(None)

    async def _sacrifice_first_idle(self, block: bool = False) -> None:
        """When trying to fill the bag, arriving at the maxsize, we may want to remove an item.
        This method try its best to find the most appropriate idle item and removes it.
        """
        eligible_obj_id, eligible_conn_or_pool = None, None

        if not self._registry:
            return

        while True:
            for obj_id, conn_or_pool in self._registry.items():
                if (
                    obj_id in self._container
                    and traffic_state_of(conn_or_pool) is TrafficState.IDLE  # type: ignore[arg-type]
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

            if block:
                break

            await asyncio.sleep(0)

        raise OverwhelmedTraffic(
            "Cannot select a disposable connection to ease the charge. "
            "This usually means that your pool sizing is insufficient, "
            "please increase your pool maxsize appropriately. "
            f"Currently set at maxsize={self.maxsize}. Usually you "
            "want as much as the number of active tasks/connections."
        )

    async def iter_idle(self) -> typing.AsyncGenerator[T, None]:
        """Iterate over idle conn contained in the container bag."""
        if self.busy:
            raise AtomicTraffic(
                "One connection/pool active per thread at a given time. "
                "Call release prior to calling this method."
            )

        if self._container:
            idle_targets: list[tuple[int, T]] = []

            for cur_obj_id, cur_conn_or_pool in self._container.items():
                if traffic_state_of(cur_conn_or_pool) is not TrafficState.IDLE:  # type: ignore[arg-type]
                    continue

                idle_targets.append((cur_obj_id, cur_conn_or_pool))

            for obj_id, conn_or_pool in idle_targets:
                del self._container[obj_id]

                if obj_id is not None and conn_or_pool is not None:
                    self._set_cursor(ActiveCursor(obj_id, conn_or_pool))

                    if self.concurrency is True:
                        self._container[obj_id] = conn_or_pool

                    try:
                        yield conn_or_pool
                    finally:
                        self.release()

    async def put(
        self,
        conn_or_pool: T,
        *traffic_indicators: MappableTraffic,
        block: bool = False,
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

        obj_id = id(conn_or_pool)
        registered_conn_or_pool = obj_id in self._registry

        if registered_conn_or_pool:
            if obj_id in self._container:
                # calling twice put? for the same conn_or_pool[...]
                # we remain conservative here on purpose. BC constraints with upstream.
                return

            active_cursor = self._cursor

            if active_cursor is not None:
                if active_cursor.obj_id != obj_id:
                    raise AtomicTraffic(
                        "You must release the previous connection prior to this."
                    )

                active_cursor.depth -= 1

                if active_cursor.depth == 0:
                    self._set_cursor(None)
        else:
            self._registry[obj_id] = conn_or_pool

        if not immediately_unavailable:
            if self._cursor is None:
                self._container[obj_id] = conn_or_pool
        else:
            self._set_cursor(ActiveCursor(obj_id, conn_or_pool))

            if self.concurrency is True and not isinstance(
                conn_or_pool, ItemPlaceholder
            ):
                self._container[obj_id] = conn_or_pool

        if traffic_indicators:
            for indicator in traffic_indicators:
                self.memorize(indicator, conn_or_pool)

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
        non_saturated_only: bool = False,
        not_idle_only: bool = False,
    ) -> T | None:
        conn_or_pool = None

        wait_clock: float = 0.0

        while True:
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
                    return None

            if self._container:
                if non_saturated_only:
                    obj_id, conn_or_pool = None, None
                    for cur_obj_id, cur_conn_or_pool in self._container.items():
                        if traffic_state_of(cur_conn_or_pool) is TrafficState.SATURATED:  # type: ignore[arg-type]
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
                            if traffic_state_of(cur_conn_or_pool) is TrafficState.IDLE:  # type: ignore[arg-type]
                                continue
                            obj_id, conn_or_pool = cur_obj_id, cur_conn_or_pool
                            break
                        if obj_id is not None:
                            del self._container[obj_id]

                if obj_id is not None and conn_or_pool is not None:
                    self._set_cursor(ActiveCursor(obj_id, conn_or_pool))

                    if self.concurrency is True:
                        self._container[obj_id] = conn_or_pool

                    break

            if conn_or_pool is None:
                if block is False:
                    before = asyncio.get_running_loop().time()
                    await asyncio.sleep(0.001)

                    if timeout is not None:
                        wait_clock += asyncio.get_running_loop().time() - before

                        if wait_clock >= timeout:
                            raise UnavailableTraffic(
                                f"No connection available within {timeout} second(s)"
                            )

                    continue

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
        block: bool = False,
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

        await self.wait_for_idle_or_available_slot()

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

            if active_cursor is None:
                raise AtomicTraffic()

            del self._registry[active_cursor.obj_id]
            self._set_cursor(None)

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
        wait_clock: float = 0.0

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

            before = asyncio.get_running_loop().time()

            await asyncio.sleep(
                0.001
            )  # 1ms is the minimum-reasonable sleep time across OSes. Will not be exactly 1ms.

            if timeout is not None:
                wait_clock += asyncio.get_running_loop().time() - before

                if wait_clock >= timeout:
                    raise TimeoutError(
                        "Timed out while waiting for conn_or_pool to become available"
                    )

        if conn_or_pool is None or obj_id is None:
            return None

        while True:
            active_cursor = self._cursor

            if active_cursor is not None:
                if active_cursor.obj_id == obj_id:
                    active_cursor.depth += 1
                    return active_cursor.conn_or_pool
                raise AtomicTraffic(
                    "Seeking to locate a connection when having another one used, did you forget a call to release?"
                )

            if obj_id not in self._container:
                if block is False:
                    raise UnavailableTraffic("Unavailable connection")
            else:
                if self.concurrency is False:
                    del self._container[obj_id]

                self._set_cursor(ActiveCursor(obj_id, conn_or_pool))

                return conn_or_pool

            before = asyncio.get_running_loop().time()

            await asyncio.sleep(
                0.001
            )  # 1ms is the minimum-reasonable sleep time across OSes. Will not be exactly 1ms.

            if timeout is not None:
                wait_clock += asyncio.get_running_loop().time() - before

                if wait_clock >= timeout:
                    raise TimeoutError(
                        "Timed out while waiting for conn_or_pool to become available"
                    )

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
                            self._set_cursor(ActiveCursor(obj_id, conn_or_pool))

                            if self.concurrency is False:
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
            self.release()

    def release(self) -> None:
        active_cursor = self._cursor

        if active_cursor is not None:
            active_cursor.depth -= 1

            if active_cursor.depth == 0:
                if self.concurrency is False:
                    self._container[active_cursor.obj_id] = active_cursor.conn_or_pool

                self._set_cursor(None)

    async def clear(self) -> None:
        """Shutdown traffic pool."""
        planned_removal = []

        for obj_id in self._container:
            if traffic_state_of(self._container[obj_id]) is TrafficState.IDLE:  # type: ignore[arg-type]
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
                self._set_cursor(None)

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
