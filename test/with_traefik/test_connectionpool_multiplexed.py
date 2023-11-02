from __future__ import annotations

from time import time

from urllib3 import HTTPSConnectionPool, ResponsePromise

from . import TraefikTestCase


class TestConnectionPoolMultiplexed(TraefikTestCase):
    def test_multiplexing_fastest_to_slowest(self) -> None:
        with HTTPSConnectionPool(
            self.host, self.https_port, ca_certs=self.ca_authority
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
