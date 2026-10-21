from __future__ import annotations

import asyncio
import typing
from unittest.mock import Mock, patch

import pytest

from urllib3 import AsyncPoolManager, AsyncProxyManager, HttpVersion
from urllib3._async.connectionpool import AsyncHTTPConnectionPool
from urllib3._async.response import AsyncHTTPResponse
from urllib3.backend import ResponsePromise
from urllib3.contrib.webextensions._async import (
    AsyncFastWebSocketExtensionFromHTTP,
    AsyncWebSocketExtensionFromHTTP,
)
from urllib3.util.retry import Retry

if typing.TYPE_CHECKING:
    from typing_extensions import Literal


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://@", None])
async def test_nohost(url: str | None) -> None:
    from urllib3.exceptions import LocationValueError

    async with AsyncPoolManager() as manager:
        with pytest.raises(LocationValueError, match="No host specified"):
            await manager.connection_from_url(url)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["ws", "wss"])
@pytest.mark.parametrize("disabled_svn", [(), (HttpVersion.h3,)])
@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize("http_first", [False, True])
async def test_extension_protocol_restrictions_are_local(
    scheme: str,
    disabled_svn: tuple[HttpVersion, ...],
    override: bool,
    http_first: bool,
) -> None:
    if (
        AsyncWebSocketExtensionFromHTTP is None
        and AsyncFastWebSocketExtensionFromHTTP is None
    ):
        pytest.skip("test requires a WebSocket backend")
    caller_options = set(disabled_svn)
    manager_options = {HttpVersion.h11} if override else caller_options
    expected_options = manager_options.copy()
    pool_kwargs = {"disabled_svn": caller_options} if override else None
    http_scheme = "https" if scheme == "wss" else "http"
    url = f"{http_scheme}://example.test/"

    async with AsyncPoolManager(disabled_svn=manager_options) as manager:
        existing = await manager.connection_from_url(url) if http_first else None
        websocket = await manager.connection_from_url(
            f"{scheme}://example.test/", pool_kwargs=pool_kwargs
        )
        assert caller_options == set(disabled_svn)
        assert manager.connection_pool_kw["disabled_svn"] == expected_options
        assert websocket.conn_kw["disabled_svn"] == set(disabled_svn) | {
            HttpVersion.h2,
            HttpVersion.h3,
        }
        normal = await manager.connection_from_url(url)
        assert normal is not websocket
        assert normal.conn_kw["disabled_svn"] == expected_options
        if existing is not None:
            assert normal is existing
            assert existing.conn_kw["disabled_svn"] == expected_options
        assert await manager.connection_from_url(url) is normal
        assert (
            await manager.connection_from_url(
                f"{scheme}://example.test/", pool_kwargs=pool_kwargs
            )
            is websocket
        )
        other = await manager.connection_from_url(f"{http_scheme}://other.test/")
        assert other.conn_kw["disabled_svn"] == expected_options
        assert manager.pools.rsize() == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("proxy_host", [None, "proxy", "[fe80::2%25251]"])
@pytest.mark.parametrize("zone", ["251", "25ethA"])
async def test_scoped_ipv6_request_pool(
    scheme: str, proxy_host: str | None, zone: str
) -> None:
    manager = (
        AsyncProxyManager(f"http://{proxy_host}:8080")
        if proxy_host
        else AsyncPoolManager()
    )
    request = Mock()

    async def urlopen(
        self: AsyncHTTPConnectionPool, *args: typing.Any, **kwargs: typing.Any
    ) -> AsyncHTTPResponse:
        request(self, *args, **kwargs)
        return AsyncHTTPResponse(status=200)

    async with manager:
        with patch.object(AsyncHTTPConnectionPool, "urlopen", urlopen):
            response = await manager.urlopen("GET", f"{scheme}://[fe80::1%25{zone}]/")
        assert response.status == 200
        pool = request.call_args[0][0]
        if proxy_host and scheme == "http":
            assert pool.host == ("proxy" if proxy_host == "proxy" else "fe80::2%251")
        else:
            assert pool.host == f"fe80::1%{zone}"
            assert pool._tunnel_host == f"[fe80::1%{zone}]"


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["http", "https", "ws", "wss"])
@pytest.mark.parametrize("from_url", [False, True])
@pytest.mark.parametrize(
    "zone, other_zone",
    [("ethA", "etha"), ("251", "1"), ("25ethA", "ethA"), ("25251", "251")],
)
async def test_scoped_ipv6_pool_key_preserves_zone_case(
    scheme: str, from_url: bool, zone: str, other_zone: str
) -> None:
    if (
        scheme in ("ws", "wss")
        and AsyncWebSocketExtensionFromHTTP is None
        and AsyncFastWebSocketExtensionFromHTTP is None
    ):
        pytest.skip("test requires a WebSocket backend")
    async with AsyncPoolManager() as manager:
        pools = []
        for host in (f"FE80::1%{zone}", f"fe80::1%{other_zone}", f"fe80::1%{zone}"):
            if from_url:
                url = f"{scheme}://[{host.replace('%', '%25')}]:8080/"
                pools.append(await manager.connection_from_url(url))
            else:
                pools.append(await manager.connection_from_host(host, 8080, scheme))
        assert pools[0] is not pools[1]
        assert pools[0] is pools[2]
        assert pools[0].host == f"fe80::1%{zone}"
        assert pools[1].host == f"fe80::1%{other_zone}"


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
