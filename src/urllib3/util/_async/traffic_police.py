from __future__ import annotations

import asyncio
import contextlib
import contextvars
import typing

from ..traffic_police import (
    AtomicTraffic,
    OverwhelmedTraffic,
    TrafficState,
    UnavailableTraffic,
    traffic_state_of,
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


class AsyncTrafficPolice(typing.Generic[T]):
    """Task-safe extended-LIFO implementation.

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

        self.__ctx_cursor: contextvars.ContextVar[
            tuple[int, T] | None
        ] = contextvars.ContextVar("cursor", default=None)
        self.__ctx_wait_clock: contextvars.ContextVar[
            float | None
        ] = contextvars.ContextVar("wait_clock", default=None)

    @property
    def _cursor(self) -> tuple[int, T] | None:
        try:
            return self.__ctx_cursor.get()
        except LookupError:
            return None

    @property
    def _wait_clock(self) -> float | None:
        try:
            return self.__ctx_wait_clock.get()
        except LookupError:
            return None

    def _set_cursor(self, value: tuple[int, T] | None) -> None:
        self.__ctx_cursor.set(value)

    def _set_wait_clock(self, value: float | None) -> None:
        self.__ctx_wait_clock.set(value)

    @property
    def busy(self) -> bool:
        return self._cursor is not None

    @property
    def bag_only_idle(self) -> bool:
        return all(
            traffic_state_of(_) == TrafficState.IDLE for _ in self._registry.values()
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
                    traffic_state_of(conn_or_pool) != TrafficState.SATURATED  # type: ignore[arg-type]
                    and obj_id in self._container
                ):
                    return

            await asyncio.sleep(0.001)
            combined_wait += 0.001

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
                    traffic_state_of(conn_or_pool) == TrafficState.IDLE  # type: ignore[arg-type]
                    and obj_id in self._container
                ):
                    return

            await asyncio.sleep(0.001)
            combined_wait += 0.001

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
                    if hasattr(conn_or_pool, "is_idle") and conn_or_pool.is_idle:
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
        if self._cursor is None:
            return

        obj_id, conn_or_pool = self._cursor

        self._map_clear(conn_or_pool)

        del self._registry[obj_id]

        try:
            await conn_or_pool.close()
        except Exception:
            pass

        self._set_cursor(None)

    async def _sacrifice_first_idle(self) -> None:
        """When trying to fill the bag, arriving at the maxsize, we may want to remove an item.
        This method try its best to find the most appropriate idle item and removes it.
        """
        eligible_obj_id, eligible_conn_or_pool = None, None

        if len(self._registry) == 0:
            return

        for obj_id, conn_or_pool in self._registry.items():
            if (
                obj_id in self._container
                and traffic_state_of(conn_or_pool) == TrafficState.IDLE  # type: ignore[arg-type]
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

        raise OverwhelmedTraffic(
            "Cannot select a disposable connection to ease the charge"
        )

    async def iter_idle(self) -> typing.AsyncGenerator[T, None]:
        """Iterate over idle conn contained in the container bag."""
        if self.busy:
            raise AtomicTraffic(
                "One connection/pool active per thread at a given time. "
                "Call release prior to calling this method."
            )

        if len(self._container) > 0:
            obj_id, conn_or_pool = None, None

            for cur_obj_id, cur_conn_or_pool in self._container.items():
                if traffic_state_of(cur_conn_or_pool) != TrafficState.IDLE:  # type: ignore[arg-type]
                    continue

                obj_id, conn_or_pool = cur_obj_id, cur_conn_or_pool
                break

            if obj_id is not None:
                self._container.pop(obj_id)

            if obj_id is not None and conn_or_pool is not None:
                self._set_cursor((obj_id, conn_or_pool))

                if self.concurrency is True:
                    new_container = {obj_id: conn_or_pool}
                    new_container.update(self._container)
                    self._container = new_container

                yield conn_or_pool

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
            if len(self._registry) == 0:
                self._shutdown = False
            return

        if (
            self.maxsize is not None
            and len(self._registry) >= self.maxsize
            and id(conn_or_pool) not in self._registry
        ):
            await self._sacrifice_first_idle()

        obj_id = id(conn_or_pool)
        registered_conn_or_pool = obj_id in self._registry

        if registered_conn_or_pool:
            if obj_id in self._container:
                return
            if self._cursor is not None:
                taken_obj_id, taken_conn_or_pool = self._cursor
                if taken_obj_id == obj_id:
                    self._set_cursor(None)
        else:
            self._registry[obj_id] = conn_or_pool

        if not immediately_unavailable:
            new_container = {obj_id: conn_or_pool}
            new_container.update(self._container)

            self._container = new_container
        else:
            self._set_cursor((obj_id, conn_or_pool))

            if self.concurrency is True:
                new_container = {obj_id: conn_or_pool}
                new_container.update(self._container)

                self._container = new_container

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

        if timeout is not None:
            self._set_wait_clock(0.0)

        while True:
            if self._cursor is not None:
                raise AtomicTraffic(
                    "One connection/pool active per task at a given time. "
                    "Call release prior to calling this method."
                )

            # This part is ugly but set for backward compatibility
            # urllib3 used to fill the bag with 'None'. This simulates that
            # old and bad behavior.
            if len(self._container) == 0 and self.maxsize is not None:
                if self.maxsize > len(self._registry):
                    return None

            if len(self._container) > 0:
                if non_saturated_only:
                    obj_id, conn_or_pool = None, None
                    for cur_obj_id, cur_conn_or_pool in self._container.items():
                        if traffic_state_of(cur_conn_or_pool) == TrafficState.SATURATED:  # type: ignore[arg-type]
                            continue
                        obj_id, conn_or_pool = cur_obj_id, cur_conn_or_pool
                        break
                    if obj_id is not None:
                        self._container.pop(obj_id)
                else:
                    if not not_idle_only:
                        obj_id, conn_or_pool = self._container.popitem()
                    else:
                        obj_id, conn_or_pool = None, None
                        for cur_obj_id, cur_conn_or_pool in self._container.items():
                            if traffic_state_of(cur_conn_or_pool) == TrafficState.IDLE:  # type: ignore[arg-type]
                                continue
                            obj_id, conn_or_pool = cur_obj_id, cur_conn_or_pool
                            break
                        if obj_id is not None:
                            self._container.pop(obj_id)

                if obj_id is not None and conn_or_pool is not None:
                    self._set_cursor((obj_id, conn_or_pool))

                    if self.concurrency is True:
                        new_container = {obj_id: conn_or_pool}
                        new_container.update(self._container)
                        self._container = new_container

                    break

            if conn_or_pool is None:
                if block is True:
                    await asyncio.sleep(0.001)

                    if timeout is not None:
                        assert self._wait_clock is not None
                        self._set_wait_clock(self._wait_clock + 0.001)

                        if self._wait_clock >= timeout:
                            self._set_wait_clock(None)
                            raise UnavailableTraffic(
                                f"No connection available within {timeout} second(s)"
                            )

                    continue

                raise UnavailableTraffic("No connection available")

        if timeout is not None:
            self._set_wait_clock(None)

        return conn_or_pool

    def memorize(
        self, traffic_indicator: MappableTraffic, conn_or_pool: T | None = None
    ) -> None:
        if conn_or_pool is None and self._cursor is None:
            raise AtomicTraffic("No connection active on the current task")

        if conn_or_pool is None:
            obj_id, conn_or_pool = self._cursor  # type: ignore[misc]
        else:
            obj_id, conn_or_pool = id(conn_or_pool), conn_or_pool

            if obj_id not in self._registry:
                raise UnavailableTraffic(
                    "Cannot memorize traffic indicator upon unknown connection"
                )

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

        self._map.pop(key)
        self._map_types.pop(key)

    async def locate(
        self,
        traffic_indicator: MappableTraffic,
        block: bool = True,
        timeout: float | None = None,
    ) -> T | None:
        if timeout is not None and self._wait_clock is None:
            self._set_wait_clock(0.0)

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

        if conn_or_pool is None and obj_id is None:
            for r_obj_id, r_conn_or_pool in self._registry.items():
                if hasattr(r_conn_or_pool, "pool") and isinstance(
                    r_conn_or_pool.pool, AsyncTrafficPolice
                ):
                    if await r_conn_or_pool.pool.beacon(traffic_indicator):
                        conn_or_pool, obj_id = r_conn_or_pool, r_obj_id
                        break

        if conn_or_pool is None or obj_id is None:
            return None

        while True:
            if self._cursor is not None:
                cursor_obj_id, cursor_conn_or_pool = self._cursor

                if cursor_obj_id == obj_id:
                    return cursor_conn_or_pool
                raise AtomicTraffic(
                    "Seeking to locate a connection when having another one used, did you forget a call to release?"
                )

            if obj_id not in self._container:
                if block is False:
                    raise UnavailableTraffic("Unavailable connection")
            else:
                if self.concurrency is False:
                    self._container.pop(obj_id)

                self._set_cursor((obj_id, conn_or_pool))

                if timeout is not None:
                    self._set_wait_clock(None)

                return conn_or_pool

            await asyncio.sleep(
                0.001
            )  # 1ms is the minimum-reasonable sleep time across OSes. Will not be exactly 1ms.

            if timeout is not None:
                assert self._wait_clock is not None
                self._set_wait_clock(self._wait_clock + 0.001)
                if self._wait_clock >= timeout:
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

                        if self._cursor is not None:
                            (
                                cursor_obj_id,
                                cursor_conn_or_pool,
                            ) = self._cursor

                            if cursor_obj_id != obj_id:
                                raise AtomicTraffic(
                                    "Seeking to locate a connection when having another one used, did you forget a call to release?"
                                )

                        if self.concurrency is False:
                            self._container.pop(obj_id)

                        self._set_cursor((obj_id, conn_or_pool))
                else:
                    conn_or_pool = await self.locate(
                        traffic_indicator, block=block, timeout=timeout
                    )
            else:
                # simulate reentrant lock/borrow
                # get_response PM -> get_response HPM -> read R
                if self._cursor is not None:
                    obj_id, conn_or_pool = self._cursor
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
        if self._cursor is not None:
            obj_id, conn_or_pool = self._cursor

            if self.concurrency is False:
                new_container = {obj_id: conn_or_pool}
                new_container.update(self._container)

                self._container = new_container

            self._set_cursor(None)

    async def clear(self) -> None:
        """Shutdown traffic pool."""
        planned_removal = []

        for obj_id in self._container:
            if traffic_state_of(self._container[obj_id]) == TrafficState.IDLE:  # type: ignore[arg-type]
                planned_removal.append(obj_id)

        for obj_id in planned_removal:
            self._container.pop(obj_id)

        # if we can't shut down them all, we need to toggle the shutdown bit to collect and close remaining connections.
        if len(self._registry) > len(planned_removal):
            self._shutdown = True

        for obj_id in planned_removal:
            conn_or_pool = self._registry.pop(obj_id)

            try:
                await conn_or_pool.close()
            except (
                Exception
            ):  # Defensive: we are in a force shutdown loop, we shall dismiss errors here.
                pass

            self._map_clear(conn_or_pool)

        if self._cursor is not None:
            cursor_obj_id, conn_or_pool = self._cursor
            if cursor_obj_id in planned_removal:
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
