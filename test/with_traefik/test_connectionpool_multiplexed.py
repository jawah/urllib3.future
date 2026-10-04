from __future__ import annotations

from urllib.parse import urlencode
from random import randint
from test import TIMEOUT_TOLERANCE, notMacOS
from time import perf_counter, sleep

import pytest

from urllib3 import HTTPSConnectionPool, ResponsePromise, Retry
from urllib3.backend import HttpVersion
from urllib3.backend.hface import _HAS_HTTP3_SUPPORT
from urllib3.exceptions import MaxRetryError, ReadTimeoutError
from urllib3.util import Timeout

from . import TraefikTestCase


class TestConnectionPoolMultiplexed(TraefikTestCase):
    @pytest.mark.parametrize("version", [20, 30])
    @pytest.mark.parametrize("explicit_promise", [False, True])
    def test_cross_origin_promise_redirect_headers(
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
        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
            disabled_svn={
                HttpVersion.h11,
                HttpVersion.h3 if version == 20 else HttpVersion.h2,
            },
            timeout=5,
        ) as pool:
            promise = pool.urlopen(
                "GET",
                "/redirect-to?" + urlencode({"url": target, "status_code": 302}),
                headers=headers,
                multiplexed=True,
                assert_same_host=False,
            )
            assert isinstance(promise, ResponsePromise)
            response = pool.get_response(promise=promise if explicit_promise else None)
            assert response is not None and response.status == 200
            assert response.version == version
            echoed = {
                name.lower(): value
                for name, value in (response.json())["headers"].items()
            }
            assert echoed["x-public"] == ["retained"]
            assert (
                not {"authorization", "cookie", "proxy-authorization"} & echoed.keys()
            )
            assert headers["Authorization"] == "Bearer secret"

    @pytest.mark.parametrize("version", [20, 30])
    @pytest.mark.parametrize("read_timeout", [0.05, None])
    def test_response_promise_read_timeout(
        self, version: int, read_timeout: float | None
    ) -> None:
        if version == 30 and not _HAS_HTTP3_SUPPORT():
            pytest.skip("HTTP/3 requires qh3")
        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
            disabled_svn={
                HttpVersion.h11,
                HttpVersion.h3 if version == 20 else HttpVersion.h2,
            },
            maxsize=1,
            timeout=5,
        ) as pool:
            assert pool.urlopen("GET", "/get").version == version
            try:
                promise = pool.urlopen(
                    "GET",
                    "/delay/1",
                    multiplexed=True,
                    retries=0,
                    timeout=Timeout(connect=0.75, read=read_timeout),
                )
                assert isinstance(promise, ResponsePromise)
                # Another request must not determine this promise's read timeout.
                other = pool.urlopen("GET", "/get", multiplexed=True, timeout=0.75)
                assert isinstance(other, ResponsePromise)
                if read_timeout is None:
                    response = pool.get_response(promise=promise)
                    assert response is not None and response.status == 200
                else:
                    with pytest.raises(MaxRetryError) as caught:
                        pool.get_response(promise=promise)
                    assert isinstance(caught.value.reason, ReadTimeoutError)
                    assert "read timeout=0.05" in str(caught.value.reason)
                response = pool.get_response(promise=other)
                assert response is not None and response.status == 200
                assert pool.num_connections == 1
            finally:
                # A timed-out promise remains pending; explicitly close its connection.
                assert pool.pool is not None
                with pool.pool.borrow() as conn:
                    conn.close()

    @pytest.mark.parametrize("version", [20, 30])
    @pytest.mark.parametrize(
        "budget, backoff, succeeds",
        [(0, 0, False), (1, 0, False), (2, 0, False), (2, 1, True)],
    )
    def test_response_promise_retry_budget(
        self, version: int, budget: int, backoff: int, succeeds: bool
    ) -> None:
        if version == 30 and not _HAS_HTTP3_SUPPORT():
            pytest.skip("HTTP/3 requires qh3")
        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
            disabled_svn={
                HttpVersion.h11,
                HttpVersion.h3 if version == 20 else HttpVersion.h2,
            },
            timeout=5,
        ) as pool:
            assert pool.urlopen("GET", "/get").version == version
            retries = Retry(total=budget, backoff_factor=backoff)
            try:
                promise = pool.urlopen(
                    "GET", "/delay/1", multiplexed=True, retries=retries, timeout=0.05
                )
                assert isinstance(promise, ResponsePromise)
                if succeeds:
                    # After two timeouts, backoff allows the response to arrive.
                    response = pool.get_response(promise=promise)
                    assert response is not None and response.status == 200
                else:
                    with pytest.raises(MaxRetryError) as caught:
                        pool.get_response(promise=promise)
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
                with pool.pool.borrow() as conn:
                    conn.close()

    @notMacOS()
    def test_multiplexing_fastest_to_slowest(self) -> None:
        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=[self.test_resolver],
        ) as pool:
            promises = []

            for i in range(5):
                promise_slow = pool.urlopen("GET", "/delay/3", multiplexed=True)
                promise_fast = pool.urlopen("GET", "/delay/1", multiplexed=True)

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
        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
        ) as pool:
            promises = []

            for i in range(5):
                promise_slow = pool.urlopen(
                    "GET", "/delay/3", multiplexed=True, preload_content=False
                )
                promise_fast = pool.urlopen(
                    "GET", "/delay/1", multiplexed=True, preload_content=False
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
        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            maxsize=2,
            resolver=self.test_resolver,
        ) as pool:
            promises = []

            for i in range(300):
                promise = pool.urlopen(
                    "GET", "/delay/1", multiplexed=True, preload_content=False
                )
                assert isinstance(promise, ResponsePromise)
                promises.append(promise)

            assert len(promises) == 300
            assert pool.num_connections == 2

            for i in range(300):
                response = pool.get_response()
                assert response is not None
                assert response.status == 200
                assert "/delay/1" in response.json()["url"]

            assert pool.get_response() is None
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
    def test_multiplexing_with_redirect(
        self, depth: int, max_retries: int | None
    ) -> None:
        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver_raw,
        ) as pool:
            retry = Retry(redirect=max_retries) if max_retries is not None else None
            promise = pool.urlopen(
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
                    pool.get_response(promise=promise)
            else:
                response = pool.get_response(promise=promise)

                assert response is not None
                assert response.url is not None
                assert "/redirect" not in response.url
                assert 200 == response.status

    def test_retries_in_multiplexed_mode(self) -> None:
        with HTTPSConnectionPool(
            self.host,
            self.https_port,
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
                sleep(randint(100, 350) / 1000.0)
                promises.append(
                    pool.urlopen(
                        "GET",
                        "/unstable?failure_rate=0.4",
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
