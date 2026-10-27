from __future__ import annotations

import asyncio
import gzip
import io
import typing
from unittest.mock import Mock, patch

import pytest

from urllib3 import AsyncPoolManager, HTTPHeaderDict, Retry
from urllib3._async.connection import AsyncHTTPConnection
from urllib3._async.connectionpool import (
    AsyncHTTPConnectionPool,
    AsyncHTTPSConnectionPool,
    connection_from_url,
    idle_conn_watch_task,
)
from urllib3._async.response import AsyncHTTPResponse
from urllib3.backend import ResponsePromise
from urllib3.backend._async._base import AsyncLowLevelResponse
from urllib3.exceptions import (
    ClosedPoolError,
    HostChangedError,
    LocationValueError,
    UnrewindableBodyError,
)
from urllib3.response import HTTPResponse


@pytest.mark.asyncio
async def test_close_returns_watcher_checkout_before_draining() -> None:
    entered = asyncio.Event()

    class IdleConnection:
        is_idle = True
        last_used_at = 0.0
        closed = False

        async def peek_and_react(self) -> None:
            entered.set()
            await asyncio.Event().wait()

        async def close(self) -> None:
            self.closed = True

    pool = AsyncHTTPConnectionPool("localhost", background_watch_delay=None)
    police = pool.pool
    assert police is not None
    conn = IdleConnection()
    await police.put(conn, immediately_unavailable=True)  # type: ignore[arg-type]
    police.release()
    watcher = pool._background_monitoring = asyncio.create_task(
        idle_conn_watch_task(pool, waiting_delay=0, keepalive_delay=None)
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(pool.close(), 5)
        assert watcher.done()
        assert conn.closed
        assert police.rsize() == police.qsize() == 0
        assert not police._cursors
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        await pool.close()


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
@pytest.mark.parametrize("zone", ["1", "251", "25ethA", "25251", "etH0", "et%61"])
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
@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("zone", ["1", "251", "25ethA", "25251", "et%61"])
async def test_scoped_ipv6_factory_preserves_zone(scheme: str, zone: str) -> None:
    url = f"{scheme}://[FE80::1%25{zone}]:8080/"
    async with connection_from_url(url) as pool:
        assert pool.host == f"fe80::1%{zone}"
        assert pool._tunnel_host == f"[fe80::1%{zone}]"
        assert pool.is_same_host(url)
        assert not pool.is_same_host(f"{scheme}://[fe80::1%25other]:8080/")
        assert (await pool._new_conn()).host == f"fe80::1%{zone}"


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pool_cls", [AsyncHTTPConnectionPool, AsyncHTTPSConnectionPool]
)
async def test_response_wait_validation_and_closed_pool(pool_cls: typing.Any) -> None:
    with pytest.raises(LocationValueError):
        pool_cls("")
    async with pool_cls("localhost") as pool:
        assert await pool.get_response() is None
        with pytest.raises(TypeError, match="ResponsePromise"):
            await pool.get_response(promise=object())
        with pytest.raises(HostChangedError):
            await pool.urlopen("GET", "https://other.invalid/")
    with pytest.raises(ClosedPoolError):
        await pool.get_response()
    with pytest.raises(ClosedPoolError):
        await pool.urlopen("GET", "/")


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("status", [303, 503])
@pytest.mark.parametrize("cache", [False, True])
@pytest.mark.parametrize("preloaded", [False, True])
@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("total", [0, 1])
async def test_retry_delay_body(
    monkeypatch: pytest.MonkeyPatch,
    managed: bool,
    deferred: bool,
    status: int,
    cache: bool,
    preloaded: bool,
    compressed: bool,
    total: int,
) -> None:
    """Read real response bodies through immediate and deferred retry dispatch."""
    manager_cls: typing.Any = AsyncPoolManager
    pool_cls: typing.Any = AsyncHTTPConnectionPool
    response_cls: typing.Any = AsyncHTTPResponse
    fp_cls: typing.Any = AsyncLowLevelResponse
    owner = manager_cls() if managed else pool_cls("example.test", maxsize=1)
    payload = b'{"delay": 2.5}'
    wire_body = gzip.compress(payload) if compressed else payload
    responses: list[HTTPResponse] = []
    observed_bodies = []
    sleeps = []
    status_checks = 0

    class BodyRetry(Retry):
        def is_retry(
            self, method: str, status_code: int, has_retry_after: bool = False
        ) -> bool:
            nonlocal status_checks
            if managed and deferred and status_code == 503:
                status_checks += 1
                if status_checks == 1:
                    # Exercise the manager's own deferred status retry branch.
                    return False
            return super().is_retry(method, status_code, has_retry_after)

        def get_retry_after(self, response: HTTPResponse) -> float | None:
            # Default-off must leave an unpreloaded body discarded, even compressed.
            if not cache and not preloaded:
                observed_bodies.append(response._body)
                return None
            observed_bodies.append(response.data)
            return float(response.json()["delay"])

        async def async_get_retry_after(
            self, response: AsyncHTTPResponse
        ) -> float | None:
            if not cache and not preloaded:
                observed_bodies.append(response._body)
                return None
            observed_bodies.append(await response.data)
            return float((await response.json())["delay"])

    async def async_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("urllib3.util.retry.time.sleep", sleeps.append)
    monkeypatch.setattr("urllib3.util.retry.asyncio.sleep", async_sleep)
    try:
        if managed:
            pool = owner.connection_from_host("example.test")
            pool = await pool
        else:
            pool = owner

        def make_request(
            conn: typing.Any, method: str, url: str, **kwargs: typing.Any
        ) -> typing.Any:
            response_status = status if not responses else 200
            headers = HTTPHeaderDict({"content-length": str(len(wire_body))})
            if compressed:
                headers["content-encoding"] = "gzip"
            if response_status == 303:
                headers["location"] = "/next"
            buffer = io.BytesIO(wire_body)

            def read_body(
                amt: int | None, stream_id: int | None
            ) -> tuple[list[bytes], bool, None]:
                data = buffer.read(amt)
                return [data], buffer.tell() == len(wire_body), None

            async def async_read_body(
                amt: int | None, stream_id: int | None
            ) -> tuple[list[bytes], bool, None]:
                return read_body(amt, stream_id)

            fp = fp_cls(
                method,
                response_status,
                20 if deferred else 11,
                "",
                headers,
                async_read_body,
            )
            response = response_cls(
                body=payload if preloaded else fp,
                status=response_status,
                headers=headers,
                preload_content=False,
            )
            responses.append(response)
            if not deferred:
                return response

            # Mock only transport delivery; use actual promise registration and retries.
            promise = ResponsePromise(conn, len(responses), [])

            async def async_getresponse(**_kwargs: typing.Any) -> typing.Any:
                return response

            monkeypatch.setattr(conn, "getresponse", async_getresponse)
            return promise

        async def async_make_request(
            *args: typing.Any, **kwargs: typing.Any
        ) -> typing.Any:
            return make_request(*args, **kwargs)

        monkeypatch.setattr(pool, "_make_request", async_make_request)
        response = owner.urlopen(
            "GET",
            "http://example.test/start" if managed else "/start",
            retries=BodyRetry(
                total=total,
                status_forcelist=[503],
                cache_response_body=cache,
                raise_on_status=False,
                raise_on_redirect=False,
            ),
            preload_content=False,
            multiplexed=deferred,
        )
        response = await response
        if deferred:
            response = owner.get_response(promise=response)
            response = await response
        assert response.status == (200 if total else status)
        assert len(responses) == total + 1
        if not total or (managed and not deferred and status == 303):
            # The existing manager redirect path does not call retry-delay hooks.
            assert observed_bodies == []
        else:
            assert observed_bodies == [payload if cache or preloaded else None]
            assert [delay for delay in sleeps if delay] == (
                [2.5] if cache or preloaded else []
            )
        # Final responses, including exhausted retries, remain available to the caller.
        if not preloaded:
            assert response._body is None
            assert response.tell() == 0
    finally:
        closing = owner.clear() if managed else owner.close()
        await closing


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize(
    "status, body_kind",
    [
        (303, "file"),
        (303, "auto_file"),
        (303, "iterable"),
        (307, "file"),
        (307, "iterable"),
        (308, "file"),
        (308, "iterable"),
    ],
)
@pytest.mark.parametrize("chunked_via", ["kwarg", "header"])
async def test_redirect_body_state(
    monkeypatch: pytest.MonkeyPatch,
    managed: bool,
    deferred: bool,
    status: int,
    body_kind: str,
    chunked_via: str,
) -> None:
    """Exercise immediate/deferred redirects through both pools and managers."""
    manager_cls: typing.Any = AsyncPoolManager
    pool_cls: typing.Any = AsyncHTTPConnectionPool
    response_cls: typing.Any = AsyncHTTPResponse
    owner = manager_cls() if managed else pool_cls("example.test")
    headers = {"Content-Type": "application/octet-stream", "X-Keep": "yes"}
    if chunked_via == "header":
        headers["tRaNsFeR-EnCoDiNg"] = "chunked"
    original_headers = headers.copy()
    body = (b"pay", b"load") if body_kind == "iterable" else io.BytesIO(b"payload")
    requests: list[tuple[str, bytes, bool, HTTPHeaderDict]] = []

    try:
        if managed:
            pool = owner.connection_from_host("example.test")
            pool = await pool
        else:
            pool = owner

        def make_request(
            conn: typing.Any, method: str, url: str, **kwargs: typing.Any
        ) -> typing.Any:
            outgoing_body = kwargs["body"]
            if outgoing_body is None:
                data = b""
            elif hasattr(outgoing_body, "read"):
                data = outgoing_body.read()
            else:
                data = b"".join(outgoing_body)
            requests.append(
                (method, data, kwargs["chunked"], HTTPHeaderDict(kwargs["headers"]))
            )
            response = response_cls(
                status=status if len(requests) == 1 else 200,
                headers={"location": "/next"} if len(requests) == 1 else {},
            )
            if not deferred:
                return response

            # Supply protocol responses while exercising real promise registration,
            # get_response(), and redirect dispatch above the transport boundary.
            promise = ResponsePromise(conn, len(requests), [])

            async def async_getresponse(**_kwargs: typing.Any) -> typing.Any:
                return response

            monkeypatch.setattr(conn, "getresponse", async_getresponse)
            return promise

        async def async_make_request(
            *args: typing.Any, **kwargs: typing.Any
        ) -> typing.Any:
            return make_request(*args, **kwargs)

        monkeypatch.setattr(pool, "_make_request", async_make_request)
        response = owner.urlopen(
            "POST",
            "http://example.test/start" if managed else "/start",
            body=body,
            body_pos=0 if body_kind == "file" else None,
            headers=headers,
            chunked=chunked_via == "kwarg",
            retries=Retry(total=1, redirect=1),
            multiplexed=deferred,
        )
        response = await response
        if deferred:
            response = owner.get_response(promise=response)
            response = await response
        assert response.status == 200
    finally:
        closing = owner.clear() if managed else owner.close()
        await closing

    assert len(requests) == 2
    assert requests[0][0:2] == ("POST", b"payload")
    method, data, chunked, redirected_headers = requests[1]
    if status == 303:
        assert (method, data, chunked) == ("GET", b"", False)
        assert "Transfer-Encoding" not in redirected_headers
        assert "Content-Type" not in redirected_headers
    else:
        assert (method, data, chunked) == ("POST", b"payload", chunked_via == "kwarg")
        assert redirected_headers == HTTPHeaderDict(original_headers)
    assert redirected_headers["X-Keep"] == "yes"
    assert headers == original_headers
