from __future__ import annotations

import asyncio
import typing
from asyncio import sleep
from random import randint
from time import perf_counter

import pytest

from test import TIMEOUT_TOLERANCE, notMacOS
from urllib3 import (
    AsyncHTTPConnectionPool,
    AsyncHTTPResponse,
    AsyncHTTPSConnectionPool,
    AsyncPoolManager,
    ConnectionInfo,
    HttpVersion,
    ResponsePromise,
    Retry,
)
from urllib3._async.connection import AsyncHTTPConnection
from urllib3.backend.hface import _HAS_HTTP3_SUPPORT
from urllib3.exceptions import ClosedPoolError, MaxRetryError

from .. import TraefikTestCase


@pytest.mark.asyncio
class TestPoolManagerMultiplexed(TraefikTestCase):
    @pytest.mark.parametrize("version", [HttpVersion.h2, HttpVersion.h3])
    @pytest.mark.parametrize("same_origin", [False, True])
    @pytest.mark.parametrize("remove_sensitive", [False, True])
    @pytest.mark.parametrize("explicit_promise", [False, True])
    async def test_redirect_origin_and_host_header(
        self,
        version: HttpVersion,
        same_origin: bool,
        remove_sensitive: bool,
        explicit_promise: bool,
    ) -> None:
        from urllib.parse import urlencode

        if version is HttpVersion.h3 and not _HAS_HTTP3_SUPPORT():
            pytest.skip("HTTP/3 requires qh3")
        target_host = self.host if same_origin else self.alt_host
        target = f"https://{target_host}:{self.https_port}/headers"
        headers = {
            "Host": f"{self.host}:{self.https_port}",
            "Authorization": "Bearer secret",
            "Cookie": "private=yes",
            "X-Public": "retained",
        }
        retries = Retry(
            total=1,
            remove_headers_on_redirect=(
                Retry.DEFAULT_REMOVE_HEADERS_ON_REDIRECT if remove_sensitive else ()
            ),
        )
        async with AsyncPoolManager(
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver.new(),
            disabled_svn=set(HttpVersion) - {version},
            timeout=5,
        ) as manager:
            promise = await manager.urlopen(
                "GET",
                self.https_url
                + "/redirect-to?"
                + urlencode({"url": target, "status_code": 302}),
                headers=headers,
                retries=retries,
                multiplexed=True,
            )
            assert isinstance(promise, ResponsePromise)
            response = await manager.get_response(
                promise=promise if explicit_promise else None
            )
            assert response is not None and response.status == 200
            assert response.version == (20 if version is HttpVersion.h2 else 30)
            echoed = {
                key.lower(): value
                for key, value in (await response.json())["headers"].items()
            }
            assert echoed["host"] == [f"{target_host}:{self.https_port}"]
            assert echoed["x-public"] == ["retained"]
            retain_credentials = same_origin or not remove_sensitive
            assert ("authorization" in echoed) is retain_credentials
            assert ("cookie" in echoed) is retain_credentials
            assert headers["Host"] == f"{self.host}:{self.https_port}"
            assert headers["Authorization"] == "Bearer secret"

    @pytest.mark.parametrize("version", [HttpVersion.h2, HttpVersion.h3])
    @pytest.mark.parametrize("by_promise", [False, True])
    async def test_clear_preserves_uncollected_promises(
        self, version: HttpVersion, by_promise: bool
    ) -> None:
        import gc
        from weakref import ref

        if version is HttpVersion.h3 and not _HAS_HTTP3_SUPPORT():
            pytest.skip("HTTP/3 requires qh3")
        conn = None
        conn_ref = None
        try:
            async with AsyncPoolManager(
                num_pools=1,
                ca_certs=self.ca_authority,
                resolver=self.test_async_resolver.new(),
                disabled_svn=set(HttpVersion) - {version},
                timeout=5,
                retries=False,
            ) as manager:
                promises: list[ResponsePromise] = []
                for size in (4096, 8192):
                    promise = await manager.urlopen(
                        "GET",
                        f"{self.https_url}/bytes/{size}",
                        multiplexed=True,
                        preload_content=False,
                    )
                    assert isinstance(promise, ResponsePromise)
                    promises.append(promise)
                candidate = promises[0]._conn
                assert isinstance(candidate, AsyncHTTPConnection)
                conn = candidate
                del candidate
                assert promises[1]._conn is conn
                conn_ref = ref(conn)
                sock = conn.sock
                assert sock is not None
                old_pool = next(iter(manager.pools._registry.values()))
                police = old_pool.pool
                assert police is not None
                police_ref = ref(police)
                watcher = old_pool._background_monitoring
                assert watcher is not None
                await manager.clear()
                await manager.clear()
                assert watcher.done()
                with pytest.raises(ClosedPoolError):
                    await old_pool.request("GET", "/bytes/1")
                assert sock.fileno() >= 0
                # Niquests checks this before attempting even explicit retrieval.
                assert await manager.pools.beacon(ResponsePromise)

                # New requests must get an active pool while the old one drains.
                followup = await manager.request("GET", f"{self.https_url}/bytes/7")
                assert len(await followup.data) == 7
                assert followup._pool is not old_pool

                if not by_promise:
                    # Blind retrieval must survive without caller-held promises.
                    promises.clear()
                    del promise
                    conn = police = None
                    gc.collect()
                sizes = []
                for index in range(2):
                    response = await manager.get_response(
                        promise=promises[index] if by_promise else None
                    )
                    assert response is not None
                    assert response.status == 200
                    sizes.append(len(await response.read()))
                    await response.close()
                assert sorted(sizes) == [4096, 8192]
                assert sock.fileno() == -1
                police = police_ref()
                assert police is None or police.rsize() == 0
                assert id(old_pool) not in manager.pools._registry
                assert manager.pools.rsize() == 1
                assert all(pool is not old_pool for pool in manager.pools._map.values())
                again = await manager.request("GET", f"{self.https_url}/bytes/3")
                assert len(await again.data) == 3
                assert again._pool is followup._pool
                assert not await manager.pools.beacon(ResponsePromise)
        finally:
            if conn is None and conn_ref is not None:
                conn = conn_ref()
            if conn is not None:
                await conn.close()

    @pytest.mark.parametrize("version", [HttpVersion.h2, HttpVersion.h3])
    @pytest.mark.parametrize("owner_kind", ["pool", "manager", "adapter"])
    @pytest.mark.parametrize("pending_promise", [False, True])
    async def test_closed_owner_preserves_other_streams(
        self, version: HttpVersion, owner_kind: str, pending_promise: bool
    ) -> None:
        if version is HttpVersion.h3 and not _HAS_HTTP3_SUPPORT():
            pytest.skip("HTTP/3 requires qh3")
        options: dict[str, typing.Any] = dict(
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver.new(),
            disabled_svn=set(HttpVersion) - {version},
            timeout=5,
            retries=False,
        )
        owner = (
            AsyncPoolManager(**options)
            if owner_kind != "pool"
            else AsyncHTTPSConnectionPool(self.host, self.https_port, **options)
        )
        prefix = self.https_url if owner_kind != "pool" else ""
        conn = None
        try:
            async with owner:
                promises = []
                for size in (4096, 8192):
                    requester: AsyncPoolManager | AsyncHTTPConnectionPool = owner
                    if owner_kind == "adapter":
                        assert isinstance(owner, AsyncPoolManager)
                        requester = await owner.connection_from_url(self.https_url)
                    promise = await requester.urlopen(
                        "GET",
                        f"{prefix}/bytes/{size}",
                        multiplexed=True,
                        preload_content=False,
                    )
                    if owner_kind == "adapter":
                        assert isinstance(owner, AsyncPoolManager)
                        assert isinstance(requester, AsyncHTTPConnectionPool)
                        owner.pools.memorize(promise, requester)
                        owner.pools.release()
                    promises.append(promise)
                assert all(isinstance(p, ResponsePromise) for p in promises)
                first = await owner.get_response(promise=promises[0])
                second = await owner.get_response(promise=promises[1])
                assert first is not None and second is not None
                assert first.status == second.status == 200
                assert (
                    first.version
                    == second.version
                    == (20 if version is HttpVersion.h2 else 30)
                )
                candidate = first.connection
                assert isinstance(candidate, AsyncHTTPConnection)
                conn = candidate
                assert second.connection is conn
                sock = conn.sock
                assert sock is not None
                if pending_promise:
                    pending = await owner.urlopen(
                        "GET",
                        f"{prefix}/bytes/7",
                        multiplexed=True,
                        preload_content=False,
                    )
                    assert isinstance(pending, ResponsePromise)
                    assert pending._conn is conn
            assert sock.fileno() >= 0
            assert len(await first.read()) == 4096
            await first.close()
            assert conn.sock is sock and sock.fileno() >= 0
            assert len(await second.read()) == 8192
            if pending_promise:
                assert sock.fileno() >= 0
                remaining = await owner.get_response(promise=pending)
                assert remaining is not None
                assert len(await remaining.read()) == 7
                await remaining.close()
            assert sock.fileno() == -1
            await second.close()
        finally:
            if conn is not None:
                await conn.close()

    async def test_as_completed_early_exit_terminates_pending_requests(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from urllib3.exceptions import HTTPError

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
            results = await asyncio.wait_for(
                asyncio.gather(*requests, return_exceptions=True), timeout=5
            )

        # Closing the manager can fail an unformed response before cancel() reaches it.
        assert sum(isinstance(result, AsyncHTTPResponse) for result in results) == 1
        assert all(
            isinstance(result, (AsyncHTTPResponse, HTTPError, asyncio.CancelledError))
            for result in results
        ), results

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

    @pytest.mark.asyncio
    async def test_clear_wakes_pool_capacity_waiter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio
        import inspect
        from threading import Event

        from urllib3 import AsyncPoolManager

        async def call(
            fn: typing.Callable[..., typing.Any],
            *args: typing.Any,
            **kwargs: typing.Any,
        ) -> typing.Any:
            result = fn(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result

        manager_type: typing.Any = AsyncPoolManager
        manager = manager_type(
            num_pools=1,
            resolver=self.test_async_resolver,
            ca_certs=self.ca_authority,
            disabled_svn={HttpVersion.h3},
            timeout=5,
            retries=False,
        )
        promise = await call(
            manager.urlopen, "GET", self.https_url + "/bytes/13", multiplexed=True
        )
        old_pool = next(iter(manager.pools._registry.values()))
        waiting = Event()
        signals = manager.pools._signals
        owner, name = (signals, "register")
        register = getattr(owner, name)

        def record(*args: typing.Any, **kwargs: typing.Any) -> typing.Any:
            signal = register(*args, **kwargs)
            waiting.set()
            return signal

        monkeypatch.setattr(owner, name, record)
        loop = asyncio.get_running_loop()
        target = self.https_url.replace(self.host, self.alt_host) + "/bytes/7"
        worker = asyncio.create_task(manager.request("GET", target))
        try:
            assert await loop.run_in_executor(None, waiting.wait, 5)
            await call(manager.clear)
            response = await asyncio.wait_for(asyncio.shield(worker), 10)
            assert response.status == 200
            remaining = await call(manager.get_response, promise=promise)
            assert remaining is not None and remaining.status == 200
        finally:
            # Release the waiter even when testing an implementation that fails to wake it.
            await call(promise._conn.close)
            await call(manager.pools.kill, old_pool)
            worker.cancel()
            await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 10)
            await call(manager.clear)
