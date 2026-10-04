from __future__ import annotations

import asyncio
import typing
from asyncio import sleep
from random import randint
from test import TIMEOUT_TOLERANCE, notMacOS
from time import perf_counter

import pytest

from urllib3 import (
    AsyncHTTPResponse,
    AsyncPoolManager,
    ConnectionInfo,
    HttpVersion,
    ResponsePromise,
    Retry,
)
from urllib3._async.connection import AsyncHTTPConnection
from urllib3.exceptions import MaxRetryError

from .. import TraefikTestCase


@pytest.mark.asyncio
class TestPoolManagerMultiplexed(TraefikTestCase):
    async def test_as_completed_early_exit_cancels_pending_requests(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        all_connected = asyncio.Event()
        hold_responses = asyncio.Event()
        connected = 0
        first_response = True
        getresponse = AsyncHTTPConnection.getresponse

        async def on_post_connection(info: ConnectionInfo) -> None:
            nonlocal connected
            assert info.http_version == HttpVersion.h2
            connected += 1
            if connected == 15:
                all_connected.set()

        async def get_one_response(
            conn: AsyncHTTPConnection, *args: typing.Any, **kwargs: typing.Any
        ) -> AsyncHTTPResponse:
            nonlocal first_response
            if not first_response:
                # Stay inside the request's cleanup scope until cancelled,
                # regardless of how quickly Traefik sends the other responses.
                await hold_responses.wait()
            first_response = False
            await asyncio.wait_for(all_connected.wait(), timeout=5)
            return await getresponse(conn, *args, **kwargs)

        monkeypatch.setattr(AsyncHTTPConnection, "getresponse", get_one_response)
        requests = []
        try:
            async with AsyncPoolManager(
                timeout=3,
                retries=False,
                maxsize=1,
                ca_certs=self.ca_authority,
                resolver=self.test_async_resolver,
                disabled_svn={HttpVersion.h11, HttpVersion.h3},
            ) as pool:
                requests = [
                    asyncio.create_task(
                        pool.request(
                            "GET",
                            f"{self.https_url}/get",
                            on_post_connection=on_post_connection,
                        )
                    )
                    for _ in range(15)
                ]
                for future in asyncio.as_completed(requests):
                    response = await future
                    assert response.status == 200
                    assert response.version == 20
                    assert sum(not request.done() for request in requests) == 14
                    connection_pool = await pool.connection_from_url(self.https_url)
                    assert connection_pool.num_connections == 1
                    break
        finally:
            for request in requests:
                request.cancel()
            await asyncio.wait_for(
                asyncio.gather(*requests, return_exceptions=True), timeout=5
            )

        assert sum(request.cancelled() for request in requests) == 14

    @notMacOS()
    async def test_multiplexing_fastest_to_slowest(self) -> None:
        async with AsyncPoolManager(
            ca_certs=self.ca_authority,
            resolver=self.test_resolver_raw,
        ) as pool:
            promises = []

            for i in range(5):
                promise_slow = await pool.urlopen(
                    "GET", f"{self.https_url}/delay/3", multiplexed=True
                )
                promise_fast = await pool.urlopen(
                    "GET", f"{self.https_url}/delay/1", multiplexed=True
                )

                assert isinstance(promise_fast, ResponsePromise)
                assert isinstance(promise_slow, ResponsePromise)
                promises.append(promise_slow)
                promises.append(promise_fast)

            assert len(promises) == 10

            before = perf_counter()

            for i in range(5):
                response = await pool.get_response()
                assert response is not None
                assert response.status == 200
                assert "/delay/1" in (await response.json())["url"]

            assert perf_counter() - before <= 1.5 + TIMEOUT_TOLERANCE

            for i in range(5):
                response = await pool.get_response()
                assert response is not None
                assert response.status == 200
                assert "/delay/3" in (await response.json())["url"]

            assert perf_counter() - before <= 3.5 + TIMEOUT_TOLERANCE
            assert await pool.get_response() is None

    async def test_multiplexing_without_preload(self) -> None:
        async with AsyncPoolManager(
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver,
        ) as pool:
            promises = []

            for i in range(5):
                promise_slow = await pool.urlopen(
                    "GET",
                    f"{self.https_url}/delay/3",
                    multiplexed=True,
                    preload_content=False,
                )
                promise_fast = await pool.urlopen(
                    "GET",
                    f"{self.https_url}/delay/1",
                    multiplexed=True,
                    preload_content=False,
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
        async with AsyncPoolManager(
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver,
        ) as pool:
            promises = []

            for i in range(300):
                promise = await pool.urlopen(
                    "GET",
                    f"{self.https_url}/delay/1",
                    multiplexed=True,
                    preload_content=False,
                )
                assert isinstance(promise, ResponsePromise)
                promises.append(promise)

            assert len(promises) == 300

            for i in range(300):
                response = await pool.get_response()
                assert response is not None
                assert response.status == 200
                assert "/delay/1" in (await response.json())["url"]

            assert await pool.get_response() is None

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
        async with AsyncPoolManager(
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver,
        ) as pool:
            retry = Retry(redirect=max_retries) if max_retries is not None else None
            promise = await pool.urlopen(
                "GET",
                f"{self.https_url}/redirect/{depth}",
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
        async with AsyncPoolManager(
            ca_certs=self.ca_authority,
            resolver=[self.test_resolver_raw],
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
                        f"{self.https_url}/unstable?failure_rate=0.4",
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

    async def test_multiplexed_retry_exhausted_returns_response_when_not_raising(
        self,
    ) -> None:
        """Async mirror covering ``src/urllib3/_async/poolmanager.py`` retry-
        from-promise branch in ``AsyncPoolManager.get_response``.
        """
        async with AsyncPoolManager(
            ca_certs=self.ca_authority,
            resolver=self.test_resolver_raw,
        ) as pool:
            retry = Retry(
                1, status_forcelist=[503], backoff_factor=0.0, raise_on_status=False
            )
            promise = await pool.urlopen(
                "GET",
                f"{self.https_url}/status/503",
                retries=retry,
                multiplexed=True,
            )
            assert isinstance(promise, ResponsePromise)

            response = await pool.get_response(promise=promise)
            assert response is not None
            assert response.status == 503

    async def test_multiplexed_retry_exhausted_raises_when_configured(
        self,
    ) -> None:
        """Async companion -- exhaustion + raise_on_status=True path."""
        async with AsyncPoolManager(
            ca_certs=self.ca_authority,
            resolver=self.test_resolver_raw,
        ) as pool:
            retry = Retry(
                1, status_forcelist=[503], backoff_factor=0.0, raise_on_status=True
            )
            promise = await pool.urlopen(
                "GET",
                f"{self.https_url}/status/503",
                retries=retry,
                multiplexed=True,
            )
            assert isinstance(promise, ResponsePromise)

            with pytest.raises(MaxRetryError):
                await pool.get_response(promise=promise)

    @pytest.mark.parametrize("request_count", [1, 32])
    async def test_multiplexed_concurrent_get_response_drain(
        self, request_count: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: concurrent ``get_response()`` callers on a single
        multiplexed pool must each receive a distinct response while any
        promises remain, then ``None`` once the pool is drained.

        This guards against races in ``_find_by`` / ``TrafficPolice`` where
        two or more tasks calling ``get_response()`` simultaneously could
        otherwise corrupt the shared connection state (e.g. ``ResponseNotReady``
        or ``RecursionError`` reported under high producer count).
        """
        async with AsyncPoolManager(
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver,
            maxsize=1,
            block=True,
        ) as pool:
            promises = await asyncio.gather(
                *[
                    pool.urlopen("GET", f"{self.https_url}/get", multiplexed=True)
                    for _ in range(request_count)
                ]
            )

            assert len(promises) == request_count
            assert all(isinstance(p, ResponsePromise) for p in promises)

            connection_pool = await pool.connection_from_url(self.https_url)
            assert connection_pool.pool is not None
            signals = connection_pool.pool._signals
            register = signals.register
            readers_queued = asyncio.Event()
            getresponse = AsyncHTTPConnection.getresponse

            def on_register(*args: typing.Any) -> typing.Any:
                signal = register(*args)
                if len(signals._furthest_signals) == 3:
                    readers_queued.set()
                return signal

            async def wait_for_readers(
                conn: AsyncHTTPConnection, *args: typing.Any, **kwargs: typing.Any
            ) -> AsyncHTTPResponse:
                await asyncio.wait_for(readers_queued.wait(), 2)
                return await getresponse(conn, *args, **kwargs)

            monkeypatch.setattr(signals, "register", on_register)
            monkeypatch.setattr(AsyncHTTPConnection, "getresponse", wait_for_readers)

            for iter_count in range(16):
                responses = await asyncio.wait_for(
                    asyncio.gather(*(pool.get_response() for _ in range(4))), 5
                )
                expected = min(4, max(0, request_count - iter_count * 4))
                assert sum(r is not None for r in responses) == expected
                assert all(r.status == 200 for r in responses if r is not None)
