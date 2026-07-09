from __future__ import annotations

import asyncio

import pytest

from urllib3._async.connectionpool import AsyncHTTPConnectionPool


@pytest.mark.asyncio
async def test_legacy_queue_cls_emits_deprecation_warning() -> None:
    # Covers _async/connectionpool.py: warns + auto-fallback to AsyncTrafficPolice
    # when QueueCls is not an AsyncTrafficPolice subclass.
    import queue

    from urllib3.util._async.traffic_police import AsyncTrafficPolice

    class LegacyQueuePool(AsyncHTTPConnectionPool):
        QueueCls = queue.LifoQueue  # type: ignore[assignment]

    with pytest.warns(
        DeprecationWarning, match="QueueCls no longer support typical queue"
    ):
        pool = LegacyQueuePool(host="localhost", maxsize=1)
    try:
        assert pool.QueueCls is AsyncTrafficPolice  # type: ignore[comparison-overlap]
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_keepalive_idle_window_clamps_to_minimum() -> None:
    # Covers _async/connectionpool.py keepalive_idle_window clamp to
    # MINIMAL_KEEPALIVE_IDLE_WINDOW when an absurdly small value is given.
    from urllib3._constant import MINIMAL_KEEPALIVE_IDLE_WINDOW

    pool = AsyncHTTPConnectionPool(
        host="localhost",
        maxsize=1,
        background_watch_delay=0.1,
        keepalive_idle_window=0.001,
    )
    try:
        assert pool._keepalive_idle_window is not None
        assert pool._keepalive_idle_window >= MINIMAL_KEEPALIVE_IDLE_WINDOW
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_get_response_none_path_yields_control() -> None:
    """Regression test for https://github.com/jawah/urllib3.future/issues/384"""

    class FakeSaturatedConn:
        is_idle = False  # a pending, not fully consumed, response
        is_saturated = True  # no more concurrent stream can be opened

        async def close(self) -> None:
            return None

    pool = AsyncHTTPConnectionPool(host="localhost", maxsize=1)

    try:
        assert pool.pool is not None
        await pool.pool.put(FakeSaturatedConn())  # type: ignore[arg-type]

        assert pool.is_saturated is True
        assert pool.is_idle is False

        # the semantic contract is unchanged: nothing reapable -> None
        assert await pool.get_response() is None

        beats = 0

        async def heartbeat() -> None:
            nonlocal beats
            while True:
                beats += 1
                await asyncio.sleep(0)

        unrelated_task = asyncio.get_running_loop().create_task(heartbeat())
        await asyncio.sleep(0)  # let the heartbeat task start

        # bounded variant of the niquests saturated drain loop shape
        for _ in range(512):
            if pool.is_idle:
                break
            await pool.get_response()

        unrelated_task.cancel()

        # without the checkpoint the heartbeat task never runs (beats <= 1)
        assert beats > 1
    finally:
        await pool.close()
