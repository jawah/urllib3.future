from __future__ import annotations

import asyncio
from asyncio import sleep
from urllib.parse import urlencode
from random import randint
from test import notMacOS
from time import time

import pytest

from urllib3 import AsyncHTTPSConnectionPool, ConnectionInfo, ResponsePromise, Retry
from urllib3.backend import HttpVersion
from urllib3.backend.hface import _HAS_HTTP3_SUPPORT
from urllib3.exceptions import EmptyPoolError, MaxRetryError, ReadTimeoutError
from urllib3.util import Timeout

from .. import TraefikTestCase


@pytest.mark.asyncio
class TestConnectionPoolMultiplexed(TraefikTestCase):
    @pytest.mark.parametrize("version", [20, 30])
    @pytest.mark.parametrize("explicit_promise", [False, True])
    async def test_cross_origin_promise_redirect_headers(
        self,
        version: int,
        explicit_promise: bool,
    ) -> None:
        if version == 30 and not _HAS_HTTP3_SUPPORT():
            pytest.skip("HTTP/3 requires qh3")
        target = f"https://{self.alt_host}:{self.https_port}/headers"
        headers = {
            "Authorization": "Bearer secret",
            "Cookie": "private=yes",
            "Proxy-Authorization": "Basic secret",
            "X-Public": "retained",
        }
        async with AsyncHTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver,
            disabled_svn={
                HttpVersion.h11,
                HttpVersion.h3 if version == 20 else HttpVersion.h2,
            },
            timeout=5,
        ) as pool:
            promise = await pool.urlopen(
                "GET",
                "/redirect-to?" + urlencode({"url": target, "status_code": 302}),
                headers=headers,
                multiplexed=True,
                assert_same_host=False,
            )
            assert isinstance(promise, ResponsePromise)
            response = await pool.get_response(
                promise=promise if explicit_promise else None
            )
            assert response is not None and response.status == 200
            assert response.version == version
            echoed = {
                name.lower(): value
                for name, value in (await response.json())["headers"].items()
            }
            assert echoed["x-public"] == ["retained"]
            assert (
                not {"authorization", "cookie", "proxy-authorization"} & echoed.keys()
            )
            assert headers["Authorization"] == "Bearer secret"

    @pytest.mark.parametrize(
        "version, expected_version",
        [(HttpVersion.h11, 11), (HttpVersion.h2, 20), (HttpVersion.h3, 30)],
    )
    @pytest.mark.parametrize("cancel", [False, True], ids=["timeout", "cancel"])
    async def test_pool_recovers_after_abandoned_waiter(
        self, version: HttpVersion, expected_version: int, cancel: bool
    ) -> None:
        if version is HttpVersion.h3 and not _HAS_HTTP3_SUPPORT():
            pytest.skip("HTTP/3 requires qh3")
        held, release = asyncio.Event(), asyncio.Event()

        async def hold_connection(info: ConnectionInfo) -> None:
            held.set()
            await release.wait()

        async with AsyncHTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver,
            disabled_svn={HttpVersion.h11, HttpVersion.h2, HttpVersion.h3} - {version},
            maxsize=1,
            block=True,
            timeout=5,
            retries=False,
        ) as pool:
            assert (await pool.urlopen("GET", "/get")).version == expected_version
            assert pool.pool is not None
            owner = asyncio.create_task(
                pool.urlopen("GET", "/get", on_post_connection=hold_connection)
            )
            requests = [owner]
            try:
                await asyncio.wait_for(held.wait(), 5)
                waiter = asyncio.create_task(
                    pool.urlopen("GET", "/get", pool_timeout=None if cancel else 1)
                )
                requests.append(waiter)
                # The request reaches the occupied pool before its first suspension.
                await asyncio.sleep(0)
                assert len(pool.pool._signals._priority_signals) == 1
                if cancel:
                    waiter.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await waiter
                else:
                    with pytest.raises(EmptyPoolError):
                        await asyncio.wait_for(waiter, 5)

                release.set()
                assert (await asyncio.wait_for(owner, 5)).status == 200
                # A finished waiter must not receive this connection on release.
                for _ in range(2):
                    response = await asyncio.wait_for(
                        pool.urlopen("GET", "/get", pool_timeout=1), 5
                    )
                    assert response.status == 200
                assert pool.num_connections == 1
                assert pool.pool.qsize() == 1
                assert not pool.pool._cursors
                assert not pool.pool._signals._priority_signals
                assert not pool.pool._signals._furthest_signals
            finally:
                release.set()
                for request in requests:
                    if not request.done():
                        request.cancel()
                await asyncio.gather(*requests, return_exceptions=True)
                # Close even if a regression stranded ownership on a finished task.
                for conn in tuple(pool.pool._registry.values()):
                    await conn.close()

    @pytest.mark.parametrize("version", [20, 30])
    @pytest.mark.parametrize("read_timeout", [0.05, None])
    async def test_response_promise_read_timeout(
        self, version: int, read_timeout: float | None
    ) -> None:
        if version == 30 and not _HAS_HTTP3_SUPPORT():
            pytest.skip("HTTP/3 requires qh3")
        async with AsyncHTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver,
            disabled_svn={
                HttpVersion.h11,
                HttpVersion.h3 if version == 20 else HttpVersion.h2,
            },
            maxsize=1,
            timeout=5,
        ) as pool:
            assert (await pool.urlopen("GET", "/get")).version == version
            try:
                promise = await pool.urlopen(
                    "GET",
                    "/delay/1",
                    multiplexed=True,
                    retries=0,
                    timeout=Timeout(connect=0.75, read=read_timeout),
                )
                assert isinstance(promise, ResponsePromise)
                # Another request must not determine this promise's read timeout.
                other = await pool.urlopen(
                    "GET", "/get", multiplexed=True, timeout=0.75
                )
                assert isinstance(other, ResponsePromise)
                if read_timeout is None:
                    response = await pool.get_response(promise=promise)
                    assert response is not None and response.status == 200
                else:
                    with pytest.raises(MaxRetryError) as caught:
                        await pool.get_response(promise=promise)
                    assert isinstance(caught.value.reason, ReadTimeoutError)
                    assert "read timeout=0.05" in str(caught.value.reason)
                response = await pool.get_response(promise=other)
                assert response is not None and response.status == 200
                assert pool.num_connections == 1
            finally:
                # A timed-out promise remains pending; explicitly close its connection.
                assert pool.pool is not None
                async with pool.pool.borrow() as conn:
                    await conn.close()

    @pytest.mark.parametrize("version", [20, 30])
    @pytest.mark.parametrize(
        "budget, backoff, succeeds",
        [(0, 0, False), (1, 0, False), (2, 0, False), (2, 1, True)],
    )
    async def test_response_promise_retry_budget(
        self, version: int, budget: int, backoff: int, succeeds: bool
    ) -> None:
        if version == 30 and not _HAS_HTTP3_SUPPORT():
            pytest.skip("HTTP/3 requires qh3")
        async with AsyncHTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver,
            disabled_svn={
                HttpVersion.h11,
                HttpVersion.h3 if version == 20 else HttpVersion.h2,
            },
            timeout=5,
        ) as pool:
            assert (await pool.urlopen("GET", "/get")).version == version
            retries = Retry(total=budget, backoff_factor=backoff)
            try:
                promise = await pool.urlopen(
                    "GET", "/delay/1", multiplexed=True, retries=retries, timeout=0.05
                )
                assert isinstance(promise, ResponsePromise)
                if succeeds:
                    # After two timeouts, backoff allows the response to arrive.
                    response = await pool.get_response(promise=promise)
                    assert response is not None and response.status == 200
                else:
                    with pytest.raises(MaxRetryError) as caught:
                        await pool.get_response(promise=promise)
                    assert isinstance(caught.value.reason, ReadTimeoutError)
                remaining = promise.get_parameter("retries")
                assert isinstance(remaining, Retry)
                assert remaining.total == 0
                assert len(remaining.history) == budget
                assert all(
                    isinstance(h.error, ReadTimeoutError) for h in remaining.history
                )
                assert retries.total == budget and retries.history == ()
            finally:
                assert pool.pool is not None
                async with pool.pool.borrow() as conn:
                    await conn.close()

    @notMacOS()
    async def test_multiplexing_fastest_to_slowest(self) -> None:
        async with AsyncHTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=[self.test_async_resolver],
        ) as pool:
            promises = []

            for i in range(5):
                promise_slow = await pool.urlopen("GET", "/delay/3", multiplexed=True)
                promise_fast = await pool.urlopen("GET", "/delay/1", multiplexed=True)

                assert isinstance(promise_fast, ResponsePromise)
                assert isinstance(promise_slow, ResponsePromise)
                promises.append(promise_slow)
                promises.append(promise_fast)

            assert len(promises) == 10

            before = time()

            for i in range(5):
                response = await pool.get_response()
                assert response is not None
                assert response.status == 200
                assert "/delay/1" in (await response.json())["url"]

            assert 1.5 >= round(time() - before, 2)

            for i in range(5):
                response = await pool.get_response()
                assert response is not None
                assert response.status == 200
                assert "/delay/3" in (await response.json())["url"]

            assert 3.5 >= round(time() - before, 2)
            assert await pool.get_response() is None

    @notMacOS()
    async def test_multiplexing_without_preload(self) -> None:
        async with AsyncHTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver,
        ) as pool:
            promises = []

            for i in range(5):
                promise_slow = await pool.urlopen(
                    "GET", "/delay/3", multiplexed=True, preload_content=False
                )
                promise_fast = await pool.urlopen(
                    "GET", "/delay/1", multiplexed=True, preload_content=False
                )

                assert isinstance(promise_fast, ResponsePromise)
                assert isinstance(promise_slow, ResponsePromise)
                promises.append(promise_slow)
                promises.append(promise_fast)

            assert len(promises) == 10

            for i in range(5):
                response = await pool.get_response()
                assert response is not None
                assert response.status == 200
                assert "/delay/1" in (await response.json())["url"]

            for i in range(5):
                response = await pool.get_response()
                assert response is not None
                assert response.status == 200
                assert "/delay/3" in (await response.json())["url"]

            assert await pool.get_response() is None

    @notMacOS()
    async def test_multiplexing_stream_saturation(self) -> None:
        async with AsyncHTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            maxsize=2,
            resolver=self.test_async_resolver,
        ) as pool:
            promises = []

            for i in range(300):
                promise = await pool.urlopen(
                    "GET", "/delay/1", multiplexed=True, preload_content=False
                )
                assert isinstance(promise, ResponsePromise)
                promises.append(promise)

            assert len(promises) == 300
            assert pool.num_connections == 2

            for i in range(300):
                response = await pool.get_response()
                assert response is not None
                assert response.status == 200
                assert "/delay/1" in (await response.json())["url"]

            assert await pool.get_response() is None
            assert pool.pool is not None and pool.num_connections == 2

    @pytest.mark.parametrize(
        "depth, max_retries",
        [
            (
                1,
                None,
            ),
            (
                2,
                None,
            ),
            (
                5,
                None,
            ),
            (
                1,
                1,
            ),
            (
                2,
                2,
            ),
            (
                5,
                5,
            ),
            (
                1,
                0,
            ),
            (
                2,
                1,
            ),
            (
                5,
                4,
            ),
            (
                1,
                2,
            ),
            (
                2,
                3,
            ),
            (
                5,
                6,
            ),
        ],
    )
    async def test_multiplexing_with_redirect(
        self, depth: int, max_retries: int | None
    ) -> None:
        async with AsyncHTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver,
        ) as pool:
            retry = Retry(redirect=max_retries) if max_retries is not None else None
            promise = await pool.urlopen(
                "GET",
                f"/redirect/{depth}",
                redirect=True,
                retries=retry,
                multiplexed=True,
            )

            assert isinstance(promise, ResponsePromise)

            if (max_retries is not None and max_retries < depth) or (
                max_retries is None
                and isinstance(Retry.DEFAULT.total, int)
                and depth > Retry.DEFAULT.total
            ):
                with pytest.raises(MaxRetryError):
                    await pool.get_response(promise=promise)
            else:
                response = await pool.get_response(promise=promise)

                assert response is not None
                assert response.url is not None
                assert "/redirect" not in response.url
                assert 200 == response.status

    async def test_retries_in_multiplexed_mode(self) -> None:
        async with AsyncHTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver,
        ) as pool:
            retry = Retry(
                16, status_forcelist=[500], backoff_factor=0.05, raise_on_redirect=True
            )

            incr = 0
            bck_method = Retry.increment

            def _catch_increment_done_once(*args, **kwargs):  # type: ignore[no-untyped-def]
                nonlocal bck_method, incr
                incr += 1
                return bck_method(*args, **kwargs)

            Retry.increment = _catch_increment_done_once  # type: ignore[method-assign]

            promises = []

            for _ in range(32):
                # we need this to avoid killing the "failure_rate" respect
                # in manual multiplexed mode. it's too fast, and the rate isn't respected
                # as it should.
                await sleep(randint(100, 350) / 1000.0)
                promises.append(
                    await pool.urlopen(
                        "GET",
                        "/unstable?failure_rate=0.4",
                        redirect=True,
                        retries=retry,
                        multiplexed=True,
                    )
                )

            responses = []

            for promise in promises:
                responses.append(await pool.get_response(promise=promise))

            for response in responses:
                assert response is not None
                assert response.status == 200

            Retry.increment = bck_method  # type: ignore[method-assign]

            assert incr > 0
