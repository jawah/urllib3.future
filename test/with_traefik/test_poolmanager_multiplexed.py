from __future__ import annotations

import typing
from random import randint
from test import TIMEOUT_TOLERANCE, notMacOS
from threading import Thread
from time import perf_counter, sleep

import pytest

from urllib3 import (
    HTTPConnectionPool,
    HTTPSConnectionPool,
    HttpVersion,
    PoolManager,
    ResponsePromise,
    Retry,
)
from urllib3.backend.hface import _HAS_HTTP3_SUPPORT
from urllib3.exceptions import ClosedPoolError, MaxRetryError

from . import TraefikTestCase


class TestPoolManagerMultiplexed(TraefikTestCase):
    @pytest.mark.parametrize("version", [HttpVersion.h2, HttpVersion.h3])
    @pytest.mark.parametrize("same_origin", [False, True])
    @pytest.mark.parametrize("remove_sensitive", [False, True])
    @pytest.mark.parametrize("explicit_promise", [False, True])
    def test_redirect_origin_and_host_header(
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
        with PoolManager(
            ca_certs=self.ca_authority,
            resolver=self.test_resolver.new(),
            disabled_svn=set(HttpVersion) - {version},
            timeout=5,
        ) as manager:
            promise = manager.urlopen(
                "GET",
                self.https_url
                + "/redirect-to?"
                + urlencode({"url": target, "status_code": 302}),
                headers=headers,
                retries=retries,
                multiplexed=True,
            )
            assert isinstance(promise, ResponsePromise)
            response = manager.get_response(
                promise=promise if explicit_promise else None
            )
            assert response is not None and response.status == 200
            assert response.version == (20 if version is HttpVersion.h2 else 30)
            echoed = {
                key.lower(): value for key, value in response.json()["headers"].items()
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
    def test_clear_preserves_uncollected_promises(
        self, version: HttpVersion, by_promise: bool
    ) -> None:
        import gc
        from weakref import ref

        if version is HttpVersion.h3 and not _HAS_HTTP3_SUPPORT():
            pytest.skip("HTTP/3 requires qh3")
        conn = None
        conn_ref = None
        try:
            with PoolManager(
                num_pools=1,
                ca_certs=self.ca_authority,
                resolver=self.test_resolver.new(),
                disabled_svn=set(HttpVersion) - {version},
                timeout=5,
                retries=False,
            ) as manager:
                promises: list[ResponsePromise] = []
                for size in (4096, 8192):
                    promise = manager.urlopen(
                        "GET",
                        f"{self.https_url}/bytes/{size}",
                        multiplexed=True,
                        preload_content=False,
                    )
                    assert isinstance(promise, ResponsePromise)
                    promises.append(promise)
                conn = promises[0]._conn
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
                manager.clear()
                manager.clear()
                watcher.join(1)
                assert not watcher.is_alive()
                with pytest.raises(ClosedPoolError):
                    old_pool.request("GET", "/bytes/1")
                assert sock.fileno() >= 0
                # Niquests checks this before attempting even explicit retrieval.
                assert manager.pools.beacon(ResponsePromise)

                # New requests must get an active pool while the old one drains.
                followup = manager.request("GET", f"{self.https_url}/bytes/7")
                assert len(followup.data) == 7
                assert followup._pool is not old_pool

                if not by_promise:
                    # Blind retrieval must survive without caller-held promises.
                    promises.clear()
                    del promise
                    conn = police = None
                    gc.collect()
                sizes = []
                for index in range(2):
                    response = manager.get_response(
                        promise=promises[index] if by_promise else None
                    )
                    assert response is not None
                    assert response.status == 200
                    sizes.append(len(response.read()))
                    response.close()
                assert sorted(sizes) == [4096, 8192]
                assert sock.fileno() == -1
                police = police_ref()
                assert police is None or police.rsize() == 0
                assert id(old_pool) not in manager.pools._registry
                assert manager.pools.rsize() == 1
                assert all(pool is not old_pool for pool in manager.pools._map.values())
                again = manager.request("GET", f"{self.https_url}/bytes/3")
                assert len(again.data) == 3
                assert again._pool is followup._pool
                assert not manager.pools.beacon(ResponsePromise)
        finally:
            if conn is None and conn_ref is not None:
                conn = conn_ref()
            if conn is not None:
                conn.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("asynchronous", [False, True])
    async def test_clear_wakes_pool_capacity_waiter(
        self, asynchronous: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio
        import inspect
        from functools import partial
        from threading import Event
        from urllib3 import AsyncPoolManager

        async def call(
            fn: typing.Callable[..., typing.Any],
            *args: typing.Any,
            **kwargs: typing.Any,
        ) -> typing.Any:
            result = fn(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result

        manager_type: typing.Any = AsyncPoolManager if asynchronous else PoolManager
        manager = manager_type(
            num_pools=1,
            resolver=self.test_async_resolver if asynchronous else self.test_resolver,
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
        owner, name = (
            (signals, "register")
            if asynchronous
            else (manager.pools, "_register_signal")
        )
        register = getattr(owner, name)

        def record(*args: typing.Any, **kwargs: typing.Any) -> typing.Any:
            signal = register(*args, **kwargs)
            waiting.set()
            return signal

        monkeypatch.setattr(owner, name, record)
        loop = asyncio.get_running_loop()
        target = self.https_url.replace(self.host, self.alt_host) + "/bytes/7"
        worker = (
            asyncio.create_task(manager.request("GET", target))
            if asynchronous
            else loop.run_in_executor(None, partial(manager.request, "GET", target))
        )
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
            if asynchronous:
                worker.cancel()
            else:
                for signal in tuple(signals):
                    signal.event.set()
            await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 10)
            await call(manager.clear)

    @pytest.mark.parametrize("version", [HttpVersion.h2, HttpVersion.h3])
    @pytest.mark.parametrize("owner_kind", ["pool", "manager", "adapter"])
    @pytest.mark.parametrize("pending_promise", [False, True])
    def test_closed_owner_preserves_other_streams(
        self, version: HttpVersion, owner_kind: str, pending_promise: bool
    ) -> None:
        if version is HttpVersion.h3 and not _HAS_HTTP3_SUPPORT():
            pytest.skip("HTTP/3 requires qh3")
        options: dict[str, typing.Any] = dict(
            ca_certs=self.ca_authority,
            resolver=self.test_resolver.new(),
            disabled_svn=set(HttpVersion) - {version},
            timeout=5,
            retries=False,
        )
        owner = (
            PoolManager(**options)
            if owner_kind != "pool"
            else HTTPSConnectionPool(self.host, self.https_port, **options)
        )
        prefix = self.https_url if owner_kind != "pool" else ""
        conn = None
        try:
            with owner:
                promises = []
                for size in (4096, 8192):
                    requester: PoolManager | HTTPConnectionPool = owner
                    if owner_kind == "adapter":
                        assert isinstance(owner, PoolManager)
                        requester = owner.connection_from_url(self.https_url)
                    promise = requester.urlopen(
                        "GET",
                        f"{prefix}/bytes/{size}",
                        multiplexed=True,
                        preload_content=False,
                    )
                    if owner_kind == "adapter":
                        assert isinstance(owner, PoolManager)
                        assert isinstance(requester, HTTPConnectionPool)
                        owner.pools.memorize(promise, requester)
                        owner.pools.release()
                    promises.append(promise)
                assert all(isinstance(p, ResponsePromise) for p in promises)
                first = owner.get_response(promise=promises[0])
                second = owner.get_response(promise=promises[1])
                assert first is not None and second is not None
                assert first.status == second.status == 200
                assert (
                    first.version
                    == second.version
                    == (20 if version is HttpVersion.h2 else 30)
                )
                conn = first.connection
                assert conn is not None and second.connection is conn
                sock = conn.sock
                assert sock is not None
                if pending_promise:
                    pending = owner.urlopen(
                        "GET",
                        f"{prefix}/bytes/7",
                        multiplexed=True,
                        preload_content=False,
                    )
                    assert isinstance(pending, ResponsePromise)
                    assert pending._conn is conn
            assert sock.fileno() >= 0
            assert len(first.read()) == 4096
            first.close()
            assert conn.sock is sock and sock.fileno() >= 0
            assert len(second.read()) == 8192
            if pending_promise:
                assert sock.fileno() >= 0
                remaining = owner.get_response(promise=pending)
                assert remaining is not None
                assert len(remaining.read()) == 7
                remaining.close()
            assert sock.fileno() == -1
            second.close()
        finally:
            if conn is not None:
                conn.close()

    @notMacOS()
    def test_multiplexing_fastest_to_slowest(self) -> None:
        with PoolManager(
            ca_certs=self.ca_authority,
            resolver=self.test_resolver.new(),
        ) as pool:
            promises = []

            for i in range(5):
                promise_slow = pool.urlopen(
                    "GET", f"{self.https_url}/delay/3", multiplexed=True
                )
                promise_fast = pool.urlopen(
                    "GET", f"{self.https_url}/delay/1", multiplexed=True
                )

                assert isinstance(promise_fast, ResponsePromise)
                assert isinstance(promise_slow, ResponsePromise)
                promises.append(promise_slow)
                promises.append(promise_fast)

            assert len(promises) == 10

            before = perf_counter()

            for i in range(5):
                response = pool.get_response()
                assert response is not None
                assert response.status == 200
                assert "/delay/1" in response.json()["url"]

            assert perf_counter() - before <= 1.5 + TIMEOUT_TOLERANCE

            for i in range(5):
                response = pool.get_response()
                assert response is not None
                assert response.status == 200
                assert "/delay/3" in response.json()["url"]

            assert perf_counter() - before <= 3.5 + TIMEOUT_TOLERANCE
            assert pool.get_response() is None

    def test_multiplexing_without_preload(self) -> None:
        with PoolManager(
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
        ) as pool:
            promises = []

            for i in range(5):
                promise_slow = pool.urlopen(
                    "GET",
                    f"{self.https_url}/delay/3",
                    multiplexed=True,
                    preload_content=False,
                )
                promise_fast = pool.urlopen(
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
                response = pool.get_response()
                assert response is not None
                assert response.status == 200
                assert "/delay/1" in response.json()["url"]

            for i in range(5):
                response = pool.get_response()
                assert response is not None
                assert response.status == 200
                assert "/delay/3" in response.json()["url"]

            assert pool.get_response() is None

    @notMacOS()
    def test_multiplexing_stream_saturation(self) -> None:
        with PoolManager(
            ca_certs=self.ca_authority,
            resolver=[self.test_resolver],
        ) as pool:
            promises = []

            for i in range(300):
                promise = pool.urlopen(
                    "GET",
                    f"{self.https_url}/delay/1",
                    multiplexed=True,
                    preload_content=False,
                )
                assert isinstance(promise, ResponsePromise)
                promises.append(promise)

            assert len(promises) == 300

            for i in range(300):
                response = pool.get_response()
                assert response is not None
                assert response.status == 200
                assert "/delay/1" in response.json()["url"]

            assert pool.get_response() is None

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
    def test_multiplexing_with_redirect(
        self, depth: int, max_retries: int | None
    ) -> None:
        with PoolManager(
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
        ) as pool:
            retry = Retry(redirect=max_retries) if max_retries is not None else None
            promise = pool.urlopen(
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
                    pool.get_response(promise=promise)
            else:
                response = pool.get_response(promise=promise)

                assert response is not None
                assert response.url is not None
                assert "/redirect" not in response.url
                assert 200 == response.status

    def test_retries_in_multiplexed_mode(self) -> None:
        with PoolManager(
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
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
                sleep(randint(100, 350) / 1000.0)
                promises.append(
                    pool.urlopen(
                        "GET",
                        f"{self.https_url}/unstable?failure_rate=0.4",
                        redirect=True,
                        retries=retry,
                        multiplexed=True,
                    )
                )

            responses = []

            for promise in promises:
                responses.append(pool.get_response(promise=promise))

            for response in responses:
                assert response is not None
                assert response.status == 200

            Retry.increment = bck_method  # type: ignore[method-assign]

            assert incr > 0

    def test_multiplexed_retry_exhausted_returns_response_when_not_raising(
        self,
    ) -> None:
        """Covers the retry-from-promise branch in ``PoolManager.get_response``.
        Forces a deterministic 503 via ``/status/503`` so the retry path is taken on every attempt;
        """
        with PoolManager(
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
        ) as pool:
            retry = Retry(
                1, status_forcelist=[503], backoff_factor=0.0, raise_on_status=False
            )
            promise = pool.urlopen(
                "GET",
                f"{self.https_url}/status/503",
                retries=retry,
                multiplexed=True,
            )
            assert isinstance(promise, ResponsePromise)

            response = pool.get_response(promise=promise)
            assert response is not None
            # Retries exhausted but raise_on_status=False -> final 503 returned.
            assert response.status == 503

    def test_multiplexed_retry_exhausted_raises_when_configured(self) -> None:
        """Companion to the previous test -- with ``raise_on_status=True`` the
        exhaustion path drains the response and re-raises ``MaxRetryError``.
        """
        with PoolManager(
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
        ) as pool:
            retry = Retry(
                1, status_forcelist=[503], backoff_factor=0.0, raise_on_status=True
            )
            promise = pool.urlopen(
                "GET",
                f"{self.https_url}/status/503",
                retries=retry,
                multiplexed=True,
            )
            assert isinstance(promise, ResponsePromise)

            with pytest.raises(MaxRetryError):
                pool.get_response(promise=promise)

    def test_selected_pool_can_be_drained_by_another_reader(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event, get_ident
        from urllib3 import HTTPResponse

        selected, collected = Event(), Event()
        caller = get_ident()
        with PoolManager(
            ca_certs=self.ca_authority,
            resolver=self.test_resolver.new(),
            disabled_svn={HttpVersion.h11, HttpVersion.h3},
            timeout=5,
            retries=False,
        ) as manager:
            promise = manager.urlopen(
                "GET", self.https_url + "/bytes/7", multiplexed=True
            )
            assert isinstance(promise, ResponsePromise)
            pool = manager.connection_from_url(self.https_url)
            get_response = pool.get_response

            def after_other_reader(
                *, promise: ResponsePromise | None = None
            ) -> HTTPResponse | None:
                if get_ident() != caller:
                    selected.set()
                    assert collected.wait(5)
                return get_response(promise=promise)

            monkeypatch.setattr(pool, "get_response", after_other_reader)
            with ThreadPoolExecutor(1) as executor:
                waiting = executor.submit(manager.get_response)
                try:
                    assert selected.wait(5)
                    response = manager.get_response(promise=promise)
                    assert response is not None and response.version == 20
                    assert len(response.data) == 7
                finally:
                    collected.set()
                assert waiting.result(5) is None

    def test_multiplexed_concurrent_get_response_drain(self) -> None:
        """Regression: concurrent ``get_response()`` callers (via threads) on
        a single multiplexed pool must each receive a distinct response while
        any promises remain, then ``None`` once the pool is drained.

        Sync mirror of the async test of the same name. Guards against races
        in ``_find_by`` / ``TrafficPolice`` where two or more threads calling
        ``get_response()`` simultaneously could otherwise corrupt the shared
        connection state (e.g. ``ResponseNotReady`` or ``RecursionError``
        reported under high producer count).
        """
        with PoolManager(
            ca_certs=self.ca_authority,
            resolver=self.test_resolver.new(),
        ) as pool:
            promises = []
            for _ in range(32):
                p = pool.urlopen("GET", f"{self.https_url}/get", multiplexed=True)
                assert isinstance(p, ResponsePromise)
                promises.append(p)

            assert len(promises) == 32

            for iter_count in range(16):
                results: list[object] = [None, None, None, None]

                def _worker(idx: int) -> None:
                    results[idx] = pool.get_response()

                threads = [Thread(target=_worker, args=(i,)) for i in range(4)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

                if iter_count < 8:
                    assert all(r is not None for r in results), (
                        f"iter {iter_count}: expected 4 responses, "
                        f"got {[r is not None for r in results]}"
                    )
                    for r in results:
                        # mypy: r is not None inside this branch
                        assert getattr(r, "status", None) == 200
                else:
                    assert all(r is None for r in results), (
                        f"iter {iter_count}: expected pool drained, "
                        f"got {[r is not None for r in results]}"
                    )
