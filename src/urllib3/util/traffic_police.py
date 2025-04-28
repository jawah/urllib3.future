from __future__ import annotations

import contextlib
import queue
import time
import typing
from dataclasses import dataclass
from enum import Enum
from threading import RLock, current_thread

if typing.TYPE_CHECKING:
    from ..backend._base import ResponsePromise
    from ..connection import HTTPConnection
    from ..connectionpool import ConnectionPool
    from ..poolmanager import PoolKey
    from ..response import HTTPResponse

    MappableTraffic: typing.TypeAlias = typing.Union[
        HTTPResponse, ResponsePromise, PoolKey
    ]
    ManageableTraffic: typing.TypeAlias = typing.Union[HTTPConnection, ConnectionPool]

    T = typing.TypeVar("T", bound=ManageableTraffic)
else:
    T = typing.TypeVar("T")


class TrafficState(int, Enum):
    #: can be used to issue new request
    IDLE = 0
    #: can be used to issue new request + have stream opened
    USED = 1
    #: cannot be used to issue new request + have stream opened
    SATURATED = 2


class ItemPlaceholder:
    """A dummy placeholder to be inserted for spot reservation into the TrafficPolice."""

    @property
    def is_saturated(self) -> typing.Literal[True]:
        return True

    def close(self) -> None:
        pass


class TrafficPoliceFine(Exception): ...


class OverwhelmedTraffic(TrafficPoliceFine, queue.Full): ...


class UnavailableTraffic(TrafficPoliceFine, queue.Empty): ...


class AtomicTraffic(TrafficPoliceFine, queue.Empty): ...


def traffic_state_of(manageable_traffic: ManageableTraffic) -> TrafficState:
    if getattr(manageable_traffic, "is_saturated", False):
        return TrafficState.SATURATED
    else:
        return TrafficState.IDLE if manageable_traffic.is_idle else TrafficState.USED


@dataclass
class ActiveCursor(typing.Generic[T]):
    """Each Thread/Task is bound to a maximum of a single conn_or_pool. This represents the 'bound'."""

    obj_id: int
    conn_or_pool: T
    depth: int = 1


class TrafficPolice(typing.Generic[T]):
    """Thread-safe extended-Queue implementation.

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

        #: the registry contain the conn_or_pool administrated
        self._registry: dict[int, T] = {}
        #: the container keep available conn_or_pool
        self._container: dict[int, T] = {}
        #: keeps track of what thread hold what conn_or_pool (and depth)
        self._cursor: dict[str, ActiveCursor[T]] = {}

        #: we want to keep a mapping to know what object belong to what conn_or_pool
        #: this allows basic GPS
        self._map: dict[int | PoolKey, T] = {}
        self._map_types: dict[int | PoolKey, type] = {}

        #: the police officer is unable to do more than one thread order at a time.
        self._lock = RLock()

        #: this toggle act the progressive disposal of all conn_or_pool registered.
        self._shutdown: bool = False

        self.parent: TrafficPolice | None = None  # type: ignore[type-arg]

    @property
    def busy(self) -> bool:
        """Determine if the current thread hold a conn_or_pool that must be released asap."""
        with self._lock:
            return current_thread().name in self._cursor

    def is_held(self, conn_or_pool: T) -> bool:
        with self._lock:
            cursor_key = current_thread().name

            if cursor_key not in self._cursor:
                return False

            active_cursor = self._cursor[cursor_key]

            return active_cursor.obj_id == id(conn_or_pool)

    @property
    def bag_only_idle(self) -> bool:
        """All manageable traffic is idle. No activities for all entries."""
        with self._lock:
            return all(
                traffic_state_of(_) is TrafficState.IDLE
                for _ in self._registry.values()
            )

    @property
    def bag_only_saturated(self) -> bool:
        """All manageable traffic is saturated. No more capacity available."""
        with self._lock:
            if not self._registry:
                return False
            return all(
                traffic_state_of(_) is TrafficState.SATURATED
                for _ in self._registry.values()
            )

    def __len__(self) -> int:
        with self._lock:
            return len(self._registry)

    def _map_clear(self, value: T) -> None:
        obj_id = id(value)

        with self._lock:
            if obj_id not in self._registry:
                return

            outdated_keys = []

            for key, val in self._map.items():
                if id(val) == obj_id:
                    outdated_keys.append(key)

            for key in outdated_keys:
                del self._map[key]
                del self._map_types[key]

    def _find_by(self, traffic_type: type) -> T | None:
        """Find the first available conn or pool that is linked to at least one traffic type."""
        with self._lock:
            for k, v in self._map_types.items():
                if v is traffic_type:
                    conn_or_pool = self._map[k]

                    if conn_or_pool.is_idle is True:
                        continue
                    with self._lock:
                        obj_id = id(conn_or_pool)
                        if obj_id in self._container:
                            return conn_or_pool
            return None

    def kill_cursor(self) -> None:
        """In case there is no other way, a conn or pool may be unusable and should be destroyed.
        This make the scheduler forget about it."""
        with self._lock:
            if self.busy is False:
                return

            cursor_key = current_thread().name
            active_cursor = self._cursor[cursor_key]

            assert active_cursor is not None

            self._map_clear(active_cursor.conn_or_pool)

            if active_cursor.obj_id not in self._registry:
                raise UnavailableTraffic(
                    "Our internal thread safety mechanism seems out of sync. This is likely a bug in urllib3-future. "
                    "You may open a ticket at https://github.com/jawah/urllib3.future for support."
                )

            del self._registry[active_cursor.obj_id]

            try:
                active_cursor.conn_or_pool.close()
            except Exception:
                pass

            del self._cursor[cursor_key]

    def _sacrifice_first_idle(self, block: bool = False) -> None:
        """When trying to fill the bag, arriving at the maxsize, we may want to remove an item.
        This method try its best to find the most appropriate idle item and removes it.
        """
        eligible_obj_id, eligible_conn_or_pool = None, None

        while True:
            with self._lock:
                if self.busy is True:
                    cursor_key = current_thread().name
                    active_cursor = self._cursor[cursor_key]

                    assert active_cursor is not None

                    if (
                        traffic_state_of(active_cursor.conn_or_pool)
                        is TrafficState.IDLE
                    ):
                        self._map_clear(active_cursor.conn_or_pool)

                        del self._registry[active_cursor.obj_id]

                        try:
                            active_cursor.conn_or_pool.close()
                        except Exception:
                            pass

                        del self._cursor[cursor_key]
                        return

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
                        eligible_conn_or_pool.close()
                    except Exception:
                        pass

                    return

            if block:
                break

            time.sleep(0.001)

        raise OverwhelmedTraffic(
            "Cannot select a disposable connection to ease the charge. "
            "This usually means that your pool sizing is insufficient, "
            "please increase your pool maxsize appropriately. "
            f"Currently set at maxsize={self.maxsize}. Usually you "
            "want as much as the number of active threads/connections."
        )

    def put(
        self,
        conn_or_pool: T,
        *traffic_indicators: MappableTraffic,
        block: bool = False,
        immediately_unavailable: bool = False,
    ) -> None:
        """Register and/or store the conn_or_pool into the TrafficPolice container."""

        cursor_key = current_thread().name

        self._lock.acquire()

        # clear was called, each conn/pool that gets back must be destroyed appropriately.
        if self._shutdown is True:
            self.kill_cursor()
            # Cleanup was completed, no need to act like this anymore.
            if not self._registry:
                self._shutdown = False
            self._lock.release()
            return

        # we want to dispose of conn_or_pool surplus as soon as possible
        if (
            self.maxsize is not None
            and len(self._registry) >= self.maxsize
            and id(conn_or_pool) not in self._registry
        ):
            self._lock.release()
            self._sacrifice_first_idle(block=block)
        else:
            self._lock.release()

        with self._lock:
            obj_id = id(conn_or_pool)
            registered_conn_or_pool = obj_id in self._registry

            # if we have already seen that one beforehand.
            if registered_conn_or_pool is True:
                # calling twice put? happen when release_conn=True & calling Response::release_conn() from error handler.
                if obj_id in self._container:
                    return
            else:  # register it
                self._registry[obj_id] = conn_or_pool

            if immediately_unavailable is False:  # put back conn
                if not self.busy:
                    # that branch is mostly kept for backward compatibility
                    # with urllib3.
                    # e.g. get_conn multiple times (w/ maxsize=1), then put back any connection taken.
                    self._container[obj_id] = conn_or_pool
                else:
                    active_cursor = self._cursor[cursor_key]

                    active_cursor.depth -= 1

                    if active_cursor.depth == 0:
                        self._container[obj_id] = conn_or_pool

                        del self._cursor[cursor_key]
            else:  # we may want to declare it unavailable immediately.
                cursor_key = current_thread().name

                if cursor_key not in self._cursor:
                    self._cursor[cursor_key] = ActiveCursor(obj_id, conn_or_pool)
                else:
                    raise AtomicTraffic(
                        "You must release the previous connection in order to acquire a new one."
                    )

                if self.concurrency is True and not isinstance(
                    conn_or_pool, ItemPlaceholder
                ):
                    self._container[obj_id] = conn_or_pool

        if traffic_indicators:
            for indicator in traffic_indicators:
                self.memorize(indicator, conn_or_pool)

    def iter_idle(self) -> typing.Generator[T, None, None]:
        with self._lock:
            if self.busy is True:
                raise AtomicTraffic(
                    "One connection/pool active per thread at a given time. "
                    "Call release prior to calling this method."
                )

            if self._container:
                idle_targets: list[tuple[int, T]] = []

                for cur_obj_id, cur_conn_or_pool in self._container.items():
                    if traffic_state_of(cur_conn_or_pool) is not TrafficState.IDLE:
                        continue

                    idle_targets.append((cur_obj_id, cur_conn_or_pool))

                cursor_key = current_thread().name

                for obj_id, conn_or_pool in idle_targets:
                    del self._container[obj_id]

                    if cursor_key not in self._cursor:
                        self._cursor[cursor_key] = ActiveCursor(obj_id, conn_or_pool)
                    else:
                        # this branch SHOULD never be evaluated!
                        self._cursor[cursor_key].depth += 1

                    if self.concurrency is True:
                        self._container[obj_id] = conn_or_pool

                    try:
                        yield conn_or_pool
                    finally:
                        self.release()

    def get_nowait(
        self, non_saturated_only: bool = False, not_idle_only: bool = False
    ) -> T | None:
        return self.get(
            block=False,
            non_saturated_only=non_saturated_only,
            not_idle_only=not_idle_only,
        )

    def get(
        self,
        block: bool = True,
        timeout: float | None = None,
        non_saturated_only: bool = False,
        not_idle_only: bool = False,
    ) -> T | None:
        if self.busy is True:
            raise AtomicTraffic(
                "One connection/pool active per thread at a given time. "
                "Call release prior to calling this method."
            )

        conn_or_pool = None
        wait_clock: float = 0.0

        while True:
            with self._lock:
                # This part is ugly but set for backward compatibility
                # urllib3 used to fill the bag with 'None'. This simulates that
                # old and bad behavior.
                if (
                    not self._container or self.bag_only_saturated
                ) and self.maxsize is not None:
                    if self.maxsize > len(self._registry):
                        return None

                if self._container:
                    if non_saturated_only is True:
                        obj_id, conn_or_pool = None, None
                        for cur_obj_id, cur_conn_or_pool in self._container.items():
                            if (
                                traffic_state_of(cur_conn_or_pool)
                                is TrafficState.SATURATED
                            ):
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
                                if (
                                    traffic_state_of(cur_conn_or_pool)
                                    is TrafficState.IDLE
                                ):
                                    continue
                                obj_id, conn_or_pool = cur_obj_id, cur_conn_or_pool
                                break
                            if obj_id is not None:
                                del self._container[obj_id]

                    if obj_id is not None and conn_or_pool is not None:
                        cursor_key = current_thread().name

                        if cursor_key not in self._cursor:
                            self._cursor[cursor_key] = ActiveCursor(
                                obj_id, conn_or_pool
                            )
                        else:
                            self._cursor[cursor_key].depth += 1

                        if self.concurrency is True:
                            self._container[obj_id] = conn_or_pool

                        break

            if conn_or_pool is None:
                # hmm.. this should not exist..
                # unfortunately Requests has a wierd test case
                # that set pool_size=0 to force trigger an
                # exception. don't remove that.
                if self.maxsize == 0:
                    raise UnavailableTraffic("No connection available")

                if block is False:
                    time.sleep(0.001)

                    if timeout is not None:
                        wait_clock += 0.001

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
        with self._lock:
            if conn_or_pool is None and self.busy is False:
                raise AtomicTraffic("No connection active on this thread")

            if conn_or_pool is None:
                cursor_key = current_thread().name

                active_cursor = self._cursor[cursor_key]

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
        """Remove any referenced holder to map a traffic_indicator to his owner conn_or_pool."""
        key: PoolKey | int = (
            traffic_indicator
            if isinstance(traffic_indicator, tuple)
            else id(traffic_indicator)
        )

        with self._lock:
            if key not in self._map:
                return

            del self._map[key]
            del self._map_types[key]

        if self.parent is not None:
            try:
                self.parent.forget(traffic_indicator)
            except UnavailableTraffic:
                pass

    @contextlib.contextmanager
    def locate_or_hold(
        self,
        traffic_indicator: MappableTraffic | None = None,
        block: bool = False,
    ) -> typing.Generator[typing.Callable[[T], None] | T]:
        """Reserve a spot into the TrafficPolice instance while you construct your conn_or_pool.

        Creating a conn_or_pool may or may not take significant time, in order
        to avoid having many thread racing for TrafficPolice insert, we must
        have a way to instantly reserve a spot meanwhile we built what
        is required.
        """
        if traffic_indicator is not None:
            conn_or_pool = self.locate(traffic_indicator=traffic_indicator, block=block)

            if conn_or_pool is not None:
                yield conn_or_pool
                return

        traffic_indicators = []

        if traffic_indicator is not None:
            traffic_indicators.append(traffic_indicator)

        self.put(
            ItemPlaceholder(),  # type: ignore[arg-type]
            *traffic_indicators,
            immediately_unavailable=True,
            block=block,
        )

        swap_made: bool = False

        def inner_swap(swappable_conn_or_pool: T) -> None:
            nonlocal swap_made

            swap_made = True

            with self._lock:
                self.kill_cursor()
                self.put(
                    swappable_conn_or_pool,
                    *traffic_indicators,
                    immediately_unavailable=True,
                    block=False,
                )

        yield inner_swap

        if not swap_made:
            self.kill_cursor()

    def locate(
        self,
        traffic_indicator: MappableTraffic,
        block: bool = True,
        timeout: float | None = None,
    ) -> T | None:
        """We want to know what conn_or_pool hold ownership of traffic_indicator."""
        wait_clock = 0.0
        conn_or_pool: T | None

        while True:
            with self._lock:
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
                            r_conn_or_pool.pool, TrafficPolice
                        ):
                            if r_conn_or_pool.pool.beacon(traffic_indicator):
                                conn_or_pool, obj_id = r_conn_or_pool, r_obj_id
                                break

                if conn_or_pool is None or obj_id is None:
                    return None

                if not isinstance(conn_or_pool, ItemPlaceholder):
                    break

            time.sleep(0.001)

            if timeout is not None:
                wait_clock += 0.001
                if wait_clock >= timeout:
                    raise TimeoutError(
                        "Timed out while waiting for conn_or_pool to become available"
                    )

        cursor_key = current_thread().name

        while True:
            with self._lock:
                if self.busy:
                    active_cursor = self._cursor[cursor_key]

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

                    if cursor_key not in self._cursor:
                        self._cursor[cursor_key] = ActiveCursor(obj_id, conn_or_pool)
                    else:
                        self._cursor[cursor_key].depth += 1

                    return conn_or_pool

            time.sleep(
                0.001
            )  # 1ms is the minimum-reasonable sleep time across OSes. Will not be exactly 1ms.

            if timeout is not None:
                wait_clock += 0.001
                if wait_clock >= timeout:
                    raise TimeoutError(
                        "Timed out while waiting for conn_or_pool to become available"
                    )

    @contextlib.contextmanager
    def borrow(
        self,
        traffic_indicator: MappableTraffic | type | None = None,
        block: bool = True,
        timeout: float | None = None,
        not_idle_only: bool = False,
    ) -> typing.Generator[T, None, None]:
        try:
            cursor_key = current_thread().name

            if traffic_indicator:
                if isinstance(traffic_indicator, type):
                    with self._lock:
                        conn_or_pool = self._find_by(traffic_indicator)

                        if conn_or_pool:
                            obj_id = id(conn_or_pool)

                            if self.busy:
                                active_cursor = self._cursor[cursor_key]

                                if active_cursor.obj_id != obj_id:
                                    raise AtomicTraffic(
                                        "Seeking to locate a connection when having another one used, did you forget a call to release?"
                                    )

                            if self.concurrency is False:
                                del self._container[obj_id]

                            if cursor_key not in self._cursor:
                                self._cursor[cursor_key] = ActiveCursor(
                                    obj_id,
                                    conn_or_pool,
                                )
                            else:
                                self._cursor[cursor_key].depth += 1

                else:
                    conn_or_pool = self.locate(
                        traffic_indicator, block=block, timeout=timeout
                    )
            else:
                # simulate reentrant lock/borrow
                # get_response PM -> get_response HPM -> read R
                if self.busy:
                    active_cursor = self._cursor[cursor_key]
                    obj_id, conn_or_pool = (
                        active_cursor.obj_id,
                        active_cursor.conn_or_pool,
                    )
                    active_cursor.depth += 1
                else:
                    conn_or_pool = self.get(
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
        with self._lock:
            if not self.busy:
                # we want to allow calling release twice.
                # due to legacy urllib3 constraints[...]
                return

            cursor_key = current_thread().name
            active_cursor = self._cursor[cursor_key]

            active_cursor.depth -= 1

            if active_cursor.depth == 0:
                if self.concurrency is False:
                    self._container[active_cursor.obj_id] = active_cursor.conn_or_pool

                del self._cursor[cursor_key]

    def clear(self) -> None:
        """Shutdown traffic pool."""
        planned_removal = []

        with self._lock:
            self._shutdown = True

            for obj_id in self._container:
                if traffic_state_of(self._container[obj_id]) is TrafficState.IDLE:
                    planned_removal.append(obj_id)

            for obj_id in planned_removal:
                conn_or_pool = self._container.pop(obj_id)

                try:
                    conn_or_pool.close()
                except Exception:  # Defensive: we are in a force shutdown loop, we shall dismiss errors here.
                    pass

                self._map_clear(conn_or_pool)

                del self._registry[obj_id]

            if self.busy:
                cursor_key = current_thread().name
                active_cursor = self._cursor[cursor_key]

                if active_cursor.obj_id in planned_removal:
                    del self._cursor[cursor_key]

            # we've closed all conn/pool successfully, we can unset the shutdown toggle to
            # prevent killing incoming new conn or pool.
            if not self._registry:
                self._shutdown = False

    def qsize(self) -> int:
        with self._lock:
            return len(self._container)

    def rsize(self) -> int:
        with self._lock:
            return len(self._registry)

    def beacon(self, traffic_indicator: MappableTraffic | type) -> bool:
        """Answer the following question: Do this PoliceTraffic know about the traffic_indicator?"""
        if not isinstance(traffic_indicator, type):
            key: PoolKey | int = (
                traffic_indicator
                if isinstance(traffic_indicator, tuple)
                else id(traffic_indicator)
            )

            with self._lock:
                return key in self._map

        return self._find_by(traffic_indicator) is not None

    def __repr__(self) -> str:
        with self._lock:
            is_saturated = self.bag_only_saturated
            is_idle = not is_saturated and self.bag_only_idle

            status: str

            if is_saturated:
                status = "Saturated"
            elif is_idle:
                status = "Idle"
            else:
                status = "Used"

            return f"<TrafficPolice {self.rsize()}/{self.maxsize} ({status})>"
