from __future__ import annotations

import asyncio
import typing
from unittest.mock import Mock, patch

import pytest

from urllib3 import AsyncPoolManager, AsyncProxyManager
from urllib3._async.response import AsyncHTTPResponse
from urllib3.backend import ResponsePromise

if typing.TYPE_CHECKING:
    from typing_extensions import Literal


@pytest.mark.asyncio
@pytest.mark.parametrize("multiplexed", [False, True])
@pytest.mark.parametrize(
    "proxy_scheme, target_scheme, forwarding, expected",
    [
        ("http", "http", False, "http://localhost/pa%23th?x=%23"),
        ("https", "http", False, "http://localhost/pa%23th?x=%23"),
        ("http", "https", False, "/pa%23th?x=%23"),
        ("https", "https", False, "/pa%23th?x=%23"),
        ("https", "https", True, "https://localhost/pa%23th?x=%23"),
    ],
)
async def test_pool_request_target_strips_fragment(
    multiplexed: Literal[False, True],
    proxy_scheme: str,
    target_scheme: str,
    forwarding: bool,
    expected: str,
) -> None:
    target = f"{target_scheme}://localhost/pa%23th?x=%23#private"
    result = (
        ResponsePromise(Mock(), 1, []) if multiplexed else AsyncHTTPResponse(status=200)
    )
    request = Mock()

    async def urlopen(
        *args: typing.Any, **kwargs: typing.Any
    ) -> AsyncHTTPResponse | ResponsePromise:
        request(*args, **kwargs)
        return result

    async with AsyncProxyManager(
        f"{proxy_scheme}://proxy:8080", use_forwarding_for_https=forwarding
    ) as manager:
        pool = await manager.connection_from_url(target)
        with patch.object(pool, "urlopen", urlopen):
            assert (
                await manager.urlopen("GET", target, multiplexed=multiplexed) is result
            )
        assert request.call_args[0][1] == expected
        if isinstance(result, ResponsePromise):
            assert result.get_parameter("pm_url") == target


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
