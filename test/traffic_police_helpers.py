from __future__ import annotations

import asyncio
from queue import Queue
from threading import Event
from typing import Any, Callable

from urllib3 import AsyncHTTPConnectionPool, HTTPConnectionPool
from urllib3._async.connection import AsyncHTTPConnection
from urllib3.connection import HTTPConnection
from urllib3.util._async.traffic_police import (
    ActiveCursor,
    AsyncPendingSignal,
    AsyncSignals,
    AsyncTrafficPolice,
)
from urllib3.util.traffic_police import (
    PendingSignal,
    TrafficPolice,
    TrafficState,
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


class ObservedTrafficPolice(TrafficPolice[HTTPConnection]):
    """Observe the real scheduler without changing ownership or blocking it."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.checkpoints: dict[str, Event] = {}
        self.last_signal: PendingSignal[HTTPConnection] | None = None
        self.signal_event_factory: Callable[[], Event] | None = None

    def notify(self, name: str) -> None:
        if name in self.checkpoints:
            self.checkpoints[name].set()

    def _register_signal(
        self,
        expect_conn_or_pool: HTTPConnection | None = None,
        *states: TrafficState,
    ) -> PendingSignal[HTTPConnection]:
        signal = super()._register_signal(expect_conn_or_pool, *states)
        if self.signal_event_factory is not None:
            signal.event = self.signal_event_factory()
        self.last_signal = signal
        self.notify("queued")
        return signal

    def get(self, *args: Any, **kwargs: Any) -> HTTPConnection | None:
        self.notify("get_entered")
        try:
            result = super().get(*args, **kwargs)
        except BaseException:
            self.notify("get_failed")
            raise
        self.notify("get_returned")
        return result

    def release(self) -> None:
        super().release()
        self.notify("released")


class ObservedAsyncSignals(AsyncSignals[AsyncHTTPConnection]):
    def __init__(
        self,
        cursors: dict[asyncio.Task[Any], ActiveCursor[AsyncHTTPConnection]],
        registered: Callable[[AsyncPendingSignal[AsyncHTTPConnection]], None],
    ) -> None:
        super().__init__(cursors)
        self.registered = registered

    def register(
        self,
        target_conn_or_pool: AsyncHTTPConnection | None,
        *states: TrafficState,
    ) -> AsyncPendingSignal[AsyncHTTPConnection]:
        signal = super().register(target_conn_or_pool, *states)
        self.registered(signal)
        return signal


class ObservedAsyncTrafficPolice(AsyncTrafficPolice[AsyncHTTPConnection]):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.checkpoints: dict[str, asyncio.Event] = {}
        self.last_signal: AsyncPendingSignal[AsyncHTTPConnection] | None = None
        # Construction has no waiters yet; keep the actual cursor map and signal logic.
        self._signals = ObservedAsyncSignals(self._cursors, self.registered)

    def notify(self, name: str) -> None:
        if name in self.checkpoints:
            self.checkpoints[name].set()

    def registered(self, signal: AsyncPendingSignal[AsyncHTTPConnection]) -> None:
        self.last_signal = signal
        self.notify("queued")

    async def get(self, *args: Any, **kwargs: Any) -> AsyncHTTPConnection | None:
        self.notify("get_entered")
        try:
            result = await super().get(*args, **kwargs)
        except BaseException:
            self.notify("get_failed")
            raise
        self.notify("get_returned")
        return result

    def release(self) -> bool:
        # Remain synchronous: notification must not add a cancellation checkpoint.
        result = super().release()
        self.notify("released")
        return result


class ObservedPool(HTTPConnectionPool):
    QueueCls = ObservedTrafficPolice  # type: ignore[assignment]


class ObservedAsyncPool(AsyncHTTPConnectionPool):
    QueueCls = ObservedAsyncTrafficPolice  # type: ignore[assignment]
