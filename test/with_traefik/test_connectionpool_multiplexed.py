from __future__ import annotations

from random import randint
from test import notMacOS
from time import sleep, time

import pytest

from urllib3 import HTTPSConnectionPool, ResponsePromise, Retry
from urllib3.exceptions import MaxRetryError

from . import TraefikTestCase


class TestConnectionPoolMultiplexed(TraefikTestCase):
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

            before = time()

            for i in range(5):
                response = pool.get_response()
                assert response is not None
                assert response.status == 200
                assert "/delay/1" in response.json()["url"]

            assert 1.5 >= round(time() - before, 2)

            for i in range(5):
                response = pool.get_response()
                assert response is not None
                assert response.status == 200
                assert "/delay/3" in response.json()["url"]

            assert 3.5 >= round(time() - before, 2)
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
