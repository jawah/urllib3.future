from __future__ import annotations

import asyncio
import typing
from unittest.mock import Mock, patch

import pytest

from urllib3 import AsyncPoolManager, AsyncProxyManager
from urllib3._async.connectionpool import AsyncHTTPConnectionPool
from urllib3._async.response import AsyncHTTPResponse
from urllib3.backend import ResponsePromise
from urllib3.util.retry import Retry

if typing.TYPE_CHECKING:
    from typing_extensions import Literal


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("from_url", [False, True])
async def test_scoped_ipv6_pool_key_preserves_zone_case(
    scheme: str, from_url: bool
) -> None:
    async with AsyncPoolManager() as manager:
        pools = []
        for host in ("FE80::1%ethA", "fe80::1%etha", "fe80::1%ethA"):
            if from_url:
                url = f"{scheme}://[{host.replace('%', '%25')}]:8080/"
                pools.append(await manager.connection_from_url(url))
            else:
                pools.append(await manager.connection_from_host(host, 8080, scheme))
        assert pools[0] is not pools[1]
        assert pools[0] is pools[2]
        assert pools[0].host == "fe80::1%ethA"
        assert pools[1].host == "fe80::1%etha"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "proxy_scheme, scheme",
    [("http", "http"), ("https", "http"), ("https", "https")],
)
@pytest.mark.parametrize("zone", ["1", "25", "251", "25ethA", "et%61"])
@pytest.mark.parametrize("retry_kind", [None, "status", "connection"])
async def test_scoped_ipv6_request_target_matches_host(
    proxy_scheme: str, scheme: str, zone: str, retry_kind: str | None
) -> None:
    responses: list[AsyncHTTPResponse | Exception] = []
    if retry_kind == "status":
        responses.extend([AsyncHTTPResponse(status=503), AsyncHTTPResponse(status=503)])
    elif retry_kind == "connection":
        responses.extend([OSError("reset"), OSError("reset")])
    responses.append(AsyncHTTPResponse(status=200))
    request = Mock(side_effect=responses)

    async def make_request(
        self: AsyncHTTPConnectionPool, *args: typing.Any, **kwargs: typing.Any
    ) -> AsyncHTTPResponse:
        return request(*args, **kwargs)  # type: ignore[no-any-return]

    async with AsyncProxyManager(
        f"{proxy_scheme}://proxy:8080", use_forwarding_for_https=True
    ) as manager:
        with patch.object(AsyncHTTPConnectionPool, "_make_request", make_request):
            response = await manager.urlopen(
                "GET",
                f"{scheme}://[FE80::1%25{zone}]:8080/path?x=%23#fragment",
                retries=Retry(total=2, status_forcelist=[503]),
            )
    assert response.status == 200
    assert request.call_count == len(responses)
    for call in request.call_args_list:
        assert call[0][2] == f"{scheme}://[fe80::1%{zone}]:8080/path?x=%23"
        assert call[1]["headers"]["Host"] == f"[fe80::1%{zone}]:8080"


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
