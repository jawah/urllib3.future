from __future__ import annotations

import gzip
import io
import typing

import pytest

from urllib3 import AsyncPoolManager, HTTPHeaderDict, PoolManager, Retry
from urllib3._async.connectionpool import AsyncHTTPConnectionPool
from urllib3._async.response import AsyncHTTPResponse
from urllib3.backend import LowLevelResponse, ResponsePromise
from urllib3.backend._async._base import AsyncLowLevelResponse
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.response import HTTPResponse


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("status", [303, 503])
@pytest.mark.parametrize("cache", [False, True])
@pytest.mark.parametrize("preloaded", [False, True])
@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("total", [0, 1])
async def test_retry_delay_body(
    monkeypatch: pytest.MonkeyPatch,
    asynchronous: bool,
    managed: bool,
    deferred: bool,
    status: int,
    cache: bool,
    preloaded: bool,
    compressed: bool,
    total: int,
) -> None:
    """Read real response bodies through immediate and deferred retry dispatch."""
    manager_cls: typing.Any = AsyncPoolManager if asynchronous else PoolManager
    pool_cls: typing.Any = (
        AsyncHTTPConnectionPool if asynchronous else HTTPConnectionPool
    )
    response_cls: typing.Any = AsyncHTTPResponse if asynchronous else HTTPResponse
    fp_cls: typing.Any = AsyncLowLevelResponse if asynchronous else LowLevelResponse
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
            if asynchronous:
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
                async_read_body if asynchronous else read_body,
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

            def getresponse(**_kwargs: typing.Any) -> typing.Any:
                return response

            async def async_getresponse(**_kwargs: typing.Any) -> typing.Any:
                return response

            monkeypatch.setattr(
                conn, "getresponse", async_getresponse if asynchronous else getresponse
            )
            return promise

        async def async_make_request(
            *args: typing.Any, **kwargs: typing.Any
        ) -> typing.Any:
            return make_request(*args, **kwargs)

        monkeypatch.setattr(
            pool, "_make_request", async_make_request if asynchronous else make_request
        )
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
        if asynchronous:
            response = await response
        if deferred:
            response = owner.get_response(promise=response)
            if asynchronous:
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
        if asynchronous:
            await closing
