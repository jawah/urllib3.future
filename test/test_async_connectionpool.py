from __future__ import annotations

import asyncio
import typing
from unittest.mock import Mock, patch

import pytest

from urllib3 import Retry
from urllib3._async.connection import AsyncHTTPConnection
from urllib3._async.connectionpool import (
    AsyncHTTPConnectionPool,
    AsyncHTTPSConnectionPool,
)
from urllib3._async.response import AsyncHTTPResponse
from urllib3.backend import ResponsePromise
from urllib3.exceptions import UnrewindableBodyError
@pytest.mark.asyncio
@pytest.mark.parametrize("zone", ["251", "25ethA", "et%61"])
async def test_scoped_ipv6_target_preserved_on_multiplexed_retry(zone: str) -> None:
    requested_urls = []
    responses = iter([AsyncHTTPResponse(status=503), AsyncHTTPResponse(status=200)])

    async def make_request(
        conn: AsyncHTTPConnection, method: str, url: str, **kwargs: typing.Any
    ) -> ResponsePromise:
        requested_urls.append(url)
        return ResponsePromise(conn, len(requested_urls), [])

    async def getresponse(*args: typing.Any, **kwargs: typing.Any) -> AsyncHTTPResponse:
        return next(responses)

    async with AsyncHTTPConnectionPool("localhost") as pool:
        with patch.object(pool, "_make_request", make_request):
            with patch.object(AsyncHTTPConnection, "getresponse", getresponse):
                promise = await pool.urlopen(
                    "GET",
                    f"http://[FE80::1%25{zone}]/path#fragment",
                    assert_same_host=False,
                    multiplexed=True,
                    retries=Retry(total=1, status_forcelist=[503]),
                )
                response = await pool.get_response(promise=promise)
    assert response is not None and response.status == 200
    assert requested_urls == [f"http://[fe80::1%{zone}]/path"] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pool_cls", [AsyncHTTPConnectionPool, AsyncHTTPSConnectionPool]
)
@pytest.mark.parametrize("zone", ["1", "etH0", "et%61"])
async def test_unbracketed_scoped_ipv6(
    pool_cls: type[AsyncHTTPConnectionPool], zone: str
) -> None:
    async with pool_cls(f"FE80::1%{zone}", port=8080) as pool:
        assert pool.host == f"fe80::1%{zone}"
        assert pool._tunnel_host == f"fe80::1%{zone}"
        assert (await pool._new_conn()).host == f"fe80::1%{zone}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pool_cls", [AsyncHTTPConnectionPool, AsyncHTTPSConnectionPool]
)
async def test_scoped_ipv6_zone_case_changes_host_identity(
    pool_cls: type[AsyncHTTPConnectionPool],
) -> None:
    async with pool_cls("FE80::1%ethA", port=8080) as pool:
        assert pool.is_same_host(f"{pool.scheme}://[fe80::1%25ethA]:8080/")
        assert not pool.is_same_host(f"{pool.scheme}://[fe80::1%25etha]:8080/")


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_kind", ["status", "connection"])
async def test_scoped_ipv6_redirect_target_preserved_on_retry(retry_kind: str) -> None:
    request = Mock(
        side_effect=[
            AsyncHTTPResponse(
                status=302,
                headers={"location": "http://[FE80::1%25251]:8080/next?x=%23#fragment"},
            ),
            AsyncHTTPResponse(status=503)
            if retry_kind == "status"
            else OSError("reset"),
            AsyncHTTPResponse(status=200),
        ]
    )

    async def make_request(
        *args: typing.Any, **kwargs: typing.Any
    ) -> AsyncHTTPResponse:
        return request(*args, **kwargs)  # type: ignore[no-any-return]

    async with AsyncHTTPConnectionPool("localhost") as pool:
        with patch.object(pool, "_make_request", make_request):
            response = await pool.urlopen(
                "GET",
                "/",
                assert_same_host=False,
                retries=Retry(total=2, status_forcelist=[503]),
            )
    assert response.status == 200
    assert [call[0][2] for call in request.call_args_list] == [
        "/",
        "http://[fe80::1%251]:8080/next?x=%23",
        "http://[fe80::1%251]:8080/next?x=%23",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("absolute", [False, True])
@pytest.mark.parametrize(
    "target, expected",
    [
        ("/path#private", "/path"),
        ("/path?x=1#private", "/path?x=1"),
        ("/#private", "/"),
        ("/path?x=1#", "/path?x=1"),
        ("/pa%23th?x=%23#private", "/pa%23th?x=%23"),
        ("/path?x=1", "/path?x=1"),
    ],
)
async def test_request_target_strips_fragment(
    absolute: bool, target: str, expected: str
) -> None:
    prefix = "http://localhost" if absolute else ""
    request = Mock()

    async def make_request(
        *args: typing.Any, **kwargs: typing.Any
    ) -> AsyncHTTPResponse:
        request(*args, **kwargs)
        return AsyncHTTPResponse(status=200)

    async with AsyncHTTPConnectionPool("localhost") as pool:
        with patch.object(pool, "_make_request", make_request):
            await pool.urlopen("GET", prefix + target)
        assert request.call_args[0][2] == prefix + expected


@pytest.mark.asyncio
async def test_absolute_redirect_request_target_strips_fragment() -> None:
    responses = iter(
        [
            AsyncHTTPResponse(
                status=302,
                headers={"location": "http://localhost/next?x=%23#private"},
            ),
            AsyncHTTPResponse(status=200),
        ]
    )
    request = Mock()

    async def make_request(
        *args: typing.Any, **kwargs: typing.Any
    ) -> AsyncHTTPResponse:
        request(*args, **kwargs)
        return next(responses)

    async with AsyncHTTPConnectionPool("localhost") as pool:
        with patch.object(pool, "_make_request", make_request):
            response = await pool.urlopen("GET", "/", retries=1)
    assert response.status == 200
    assert [call[0][2] for call in request.call_args_list] == [
        "/",
        "http://localhost/next?x=%23",
    ]


@pytest.mark.asyncio
async def test_retry_with_body_that_has_tell_but_no_seek() -> None:
    """An async body with tell() but no seek() cannot be replayed on retry."""

    class TellableStream:
        def __init__(self, data: bytes) -> None:
            self._data = data
            self._pos = 0

        async def read(self, n: int = -1) -> bytes:
            if n == -1:
                chunk = self._data[self._pos :]
                self._pos = len(self._data)
            else:
                chunk = self._data[self._pos : self._pos + n]
                self._pos += len(chunk)
            return chunk

        async def tell(self) -> int:
            return self._pos

    async def make_request(*args: typing.Any, **kwargs: typing.Any) -> typing.NoReturn:
        raise OSError("connection reset")

    body = TellableStream(b"hello world")

    async with AsyncHTTPConnectionPool(host="localhost", maxsize=1) as pool:
        with patch.object(pool, "_make_request", make_request):
            with pytest.raises(
                UnrewindableBodyError, match="body does not implement seek"
            ):
                await pool.urlopen(  # type: ignore[call-overload]
                    "POST",
                    "/",
                    body=body,
                    retries=Retry(total=2, allowed_methods=["POST"]),
                    body_pos=None,
                )


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


@pytest.mark.asyncio
async def test_cancelled_connection_waiter_does_not_strand_owner() -> None:
    import asyncio
    from types import SimpleNamespace
    from typing import Any
    from urllib3._async.response import AsyncHTTPResponse
    from urllib3.util._async.traffic_police import AsyncTrafficPolice

    police: AsyncTrafficPolice[Any] = AsyncTrafficPolice(maxsize=1)
    conn = SimpleNamespace(is_idle=False, is_saturated=True)
    indicator = AsyncHTTPResponse()
    await police.put(conn, indicator, immediately_unavailable=True)
    await police.put(conn)

    async def borrow() -> None:
        async with police.borrow(indicator) as acquired:
            assert acquired is conn

    async with police.borrow(indicator):
        waiting = asyncio.create_task(borrow())
        await asyncio.sleep(0)
        assert not waiting.done()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    # A saturation notification must not pretend ownership was handed to the
    # cancelled task. The released connection must remain borrowable.
    await asyncio.wait_for(asyncio.create_task(borrow()), 1)
