from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any, Callable, cast

import pytest

from dummyserver.testcase import HTTPDummyServerTestCase
from urllib3 import AsyncHTTPConnectionPool, HTTPConnectionPool
from urllib3._async.connection import AsyncHTTPConnection
from urllib3.connection import HTTPConnection
from urllib3.exceptions import EmptyPoolError
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
    UnavailableTraffic,
)


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


class TestControlledTrafficPolice(HTTPDummyServerTestCase):
    def test_handoff_after_wait_timeout_preserves_connection(self) -> None:
        timed_out, resume_wait = Event(), Event()

        class PausedTimeout(Event):
            def wait(self, timeout: float | None = None) -> bool:
                ready = super().wait(timeout)
                if not ready:
                    # Model descheduling after the OS wait times out, before get()
                    # can reacquire its lock and inspect the handoff.
                    timed_out.set()
                    assert resume_wait.wait(10)
                return ready

        with ObservedPool(
            self.host_alt,
            self.port,
            maxsize=1,
            block=True,
            timeout=5,
            retries=False,
            background_watch_delay=None,
        ) as pool:
            assert pool.request("GET", "/").status == 200
            police = pool.pool
            assert isinstance(police, ObservedTrafficPolice)
            assert police.get() is not None
            police.signal_event_factory = PausedTimeout
            with ThreadPoolExecutor(1) as executor:
                waiting = executor.submit(pool.request, "GET", "/", pool_timeout=0.01)
                try:
                    assert timed_out.wait(10)
                    police.release()
                    resume_wait.set()
                    assert waiting.result(10).status == 200
                    assert not police._signals
                    assert not police._cursor
                    assert pool.request("GET", "/after", pool_timeout=0.1).status == 200
                finally:
                    police.signal_event_factory = None
                    resume_wait.set()
                    police.release()

    @pytest.mark.parametrize("action", ["release", "kill", "timeout"])
    def test_queued_request_recovers(self, action: str) -> None:
        with ObservedPool(
            self.host_alt,
            self.port,
            maxsize=1,
            block=True,
            timeout=5,
            retries=False,
            background_watch_delay=None,
        ) as pool:
            assert pool.request("GET", "/").status == 200
            police = pool.pool
            assert isinstance(police, ObservedTrafficPolice)
            conn = police.get()
            assert conn is not None
            queued = police.checkpoints["queued"] = Event()
            with ThreadPoolExecutor(1) as executor:
                waiting = executor.submit(
                    pool.request,
                    "GET",
                    "/",
                    pool_timeout=0.01 if action == "timeout" else 5,
                )
                try:
                    assert queued.wait(10), "The request never registered its waiter"
                    if action == "timeout":
                        with pytest.raises(EmptyPoolError):
                            waiting.result(10)
                        police.release()
                    else:
                        if action == "kill":
                            police.kill(conn)
                        else:
                            police.release()
                        assert waiting.result(10).status == 200
                    assert pool.request("GET", "/after", pool_timeout=0.1).status == 200
                    assert not police._signals
                    assert not police._cursor
                finally:
                    police.release()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["release", "kill", "timeout", "cancel"])
    async def test_async_queued_request_recovers(self, action: str) -> None:
        async with ObservedAsyncPool(
            self.host_alt,
            self.port,
            maxsize=1,
            block=True,
            timeout=5,
            retries=False,
            background_watch_delay=None,
        ) as pool:
            assert (await pool.request("GET", "/")).status == 200
            police = pool.pool
            assert isinstance(police, ObservedAsyncTrafficPolice)
            conn = await police.get()
            assert conn is not None
            queued = police.checkpoints["queued"] = asyncio.Event()
            waiting = asyncio.create_task(
                pool.request(
                    "GET", "/", pool_timeout=0.01 if action == "timeout" else 5
                )
            )
            try:
                await asyncio.wait_for(queued.wait(), 10)
                if action in ("cancel", "timeout"):
                    if action == "cancel":
                        waiting.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await waiting
                    else:
                        with pytest.raises(EmptyPoolError):
                            await asyncio.wait_for(waiting, 10)
                    assert not police._signals._priority_signals
                    assert not police._signals._furthest_signals
                    police.release()
                else:
                    if action == "kill":
                        await police.kill(conn)
                    else:
                        police.release()
                    assert (await asyncio.wait_for(waiting, 10)).status == 200
                assert not police._signals._priority_signals
                assert not police._signals._furthest_signals
                assert not police._cursors
                assert (await pool.request("GET", "/after")).status == 200
            finally:
                police.release()
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)

    @pytest.mark.parametrize("action", ["release", "kill", "timeout"])
    def test_queued_response_reader(self, action: str) -> None:
        with ObservedPool(
            self.host_alt,
            self.port,
            maxsize=1,
            block=True,
            timeout=5,
            retries=False,
            background_watch_delay=None,
        ) as pool:
            response = pool.request(
                "POST", "/echo", body=b"body", preload_content=False
            )
            police = pool.pool
            assert isinstance(police, ObservedTrafficPolice)
            conn = response.connection
            assert conn is not None
            if not police.busy:
                assert police.locate(response) is conn
            queued = police.checkpoints["queued"] = Event()

            def read() -> bytes:
                with police.borrow(
                    response, timeout=0.01 if action == "timeout" else 5
                ):
                    return response.read()

            with ThreadPoolExecutor(1) as executor:
                waiting = executor.submit(read)
                try:
                    assert queued.wait(10)
                    if action == "timeout":
                        with pytest.raises(TimeoutError):
                            waiting.result(10)
                        assert not police._signals
                        assert response.read() == b"body"
                    elif action == "kill":
                        police.kill(conn)
                        with pytest.raises(UnavailableTraffic):
                            waiting.result(10)
                    else:
                        police.release()
                        assert waiting.result(10) == b"body"
                    response.close()
                    assert pool.request("GET", "/after", pool_timeout=0.1).status == 200
                    assert not police._cursor and not police._signals
                finally:
                    police.release()
                    response.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["release", "kill", "timeout", "cancel"])
    async def test_async_queued_response_reader(self, action: str) -> None:
        async with ObservedAsyncPool(
            self.host_alt,
            self.port,
            maxsize=1,
            block=True,
            timeout=5,
            retries=False,
            background_watch_delay=None,
        ) as pool:
            response = await pool.request(
                "POST", "/echo", body=b"body", preload_content=False
            )
            police = pool.pool
            assert isinstance(police, ObservedAsyncTrafficPolice)
            conn = cast("AsyncHTTPConnection | None", response.connection)
            assert conn is not None
            if not police.busy:
                assert await police.locate(response) is conn
            queued = police.checkpoints["queued"] = asyncio.Event()

            async def read() -> bytes:
                async with police.borrow(
                    response, timeout=0.01 if action == "timeout" else 5
                ):
                    return await response.read()

            waiting = asyncio.create_task(read())
            try:
                await asyncio.wait_for(queued.wait(), 10)
                if action in ("cancel", "timeout"):
                    if action == "cancel":
                        waiting.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await waiting
                    else:
                        # Shield distinguishes a resolver timeout from our test watchdog.
                        with pytest.raises(TimeoutError):
                            await asyncio.wait_for(asyncio.shield(waiting), 2)
                        assert waiting.done(), (
                            "borrow(timeout=...) ignored its deadline"
                        )
                    assert not police._signals._priority_signals
                    assert not police._signals._furthest_signals
                    assert await response.read() == b"body"
                elif action == "kill":
                    await police.kill(conn)
                    with pytest.raises(UnavailableTraffic):
                        await asyncio.wait_for(waiting, 10)
                else:
                    police.release()
                    assert await asyncio.wait_for(waiting, 10) == b"body"
                await response.close()
                assert (
                    await pool.request("GET", "/after", pool_timeout=0.1)
                ).status == 200
                assert not police._cursors
                assert not police._signals._priority_signals
                assert not police._signals._furthest_signals
            finally:
                police.release()
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
                await response.close()
