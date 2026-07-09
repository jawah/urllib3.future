from __future__ import annotations

import asyncio

import pytest

from urllib3 import AsyncPoolManager


@pytest.mark.asyncio
async def test_get_response_none_path_yields_control() -> None:
    """Regression test for https://github.com/jawah/urllib3.future/issues/384"""
    async with AsyncPoolManager() as pm:
        # the semantic contract is unchanged: no promise pending -> None
        assert await pm.get_response() is None

        beats = 0

        async def heartbeat() -> None:
            nonlocal beats
            while True:
                beats += 1
                await asyncio.sleep(0)

        unrelated_task = asyncio.get_running_loop().create_task(heartbeat())
        await asyncio.sleep(0)  # let the heartbeat task start

        for _ in range(512):
            await pm.get_response()

        unrelated_task.cancel()

        # without the checkpoint the heartbeat task never runs (beats <= 1)
        assert beats > 1
