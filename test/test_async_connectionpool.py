from __future__ import annotations

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
