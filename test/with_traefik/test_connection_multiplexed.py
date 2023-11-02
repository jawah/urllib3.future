from __future__ import annotations

from time import time

from urllib3.connection import HTTPSConnection

from . import TraefikTestCase


class TestConnectionMultiplexed(TraefikTestCase):
    def test_multiplexing_fastest_to_slowest(self) -> None:
        conn = HTTPSConnection(self.host, self.https_port, ca_certs=self.ca_authority)

        promises = []

        for i in range(5):
            promises.append(conn.request("GET", f"/delay/{i + 1}"))
            promises.append(conn.request("GET", f"/delay/{i + 1}"))

        assert len(promises) == 10

        before = time()

        for i, expected_wait in zip(range(10), [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]):
            r = conn.getresponse()

            assert r.version == 20
            assert r.json()["url"].endswith(f"/delay/{expected_wait}")

            delay = round(time() - before, 2)

            assert expected_wait + 0.5 >= delay

        conn.close()

    def test_multiplexing_slowest_to_fastest(self) -> None:
        conn = HTTPSConnection(self.host, self.https_port, ca_certs=self.ca_authority)

        promises = []

        for i in [5, 4, 3, 2, 1]:
            promises.append(conn.request("GET", f"/delay/{i}"))
            promises.append(conn.request("GET", f"/delay/{i}"))

        assert len(promises) == 10

        before = time()

        for expected_wait in [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]:
            r = conn.getresponse()

            assert r.version == 20
            assert r.json()["url"].endswith(f"/delay/{expected_wait}")

            delay = round(time() - before, 2)

            assert expected_wait + 0.5 >= delay

        conn.close()

    def test_multiplexing_wait_for_promise(self) -> None:
        conn = HTTPSConnection(self.host, self.https_port, ca_certs=self.ca_authority)

        promises = []

        for i in [5, 4, 3, 2, 1]:
            promises.append(conn.request("GET", f"/delay/{i}"))
            promises.append(conn.request("GET", f"/delay/{i}"))

        assert len(promises) == 10

        r = conn.getresponse(promise=promises[2])  # the (first) 4 seconds delay

        assert r.version == 20
        assert r.json()["url"].endswith("/delay/4")

        # empty the promise queue
        for i in range(9):
            r = conn.getresponse()
            assert r.version == 20

        assert len(conn._promises) == 0

    def test_multiplexing_upgrade_h3(self) -> None:
        conn = HTTPSConnection(self.host, self.https_port, ca_certs=self.ca_authority)

        for i in range(3):
            conn.request("GET", "/get")

        for i in range(3):
            r = conn.getresponse()
            assert r.version == 20

        for i in range(3):
            conn.request("GET", "/get")

        for i in range(3):
            r = conn.getresponse()
            assert r.version == 30
