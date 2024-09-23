from __future__ import annotations

from time import time

import pytest

from urllib3._async.connection import AsyncHTTPSConnection

from .. import TraefikTestCase


@pytest.mark.asyncio
class TestConnectionMultiplexed(TraefikTestCase):
    async def test_multiplexing_fastest_to_slowest(self) -> None:
        conn = AsyncHTTPSConnection(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver.new(),
        )

        promises = []

        for i in range(5):
            promises.append(await conn.request("GET", f"/delay/{i + 1}"))
            promises.append(await conn.request("GET", f"/delay/{i + 1}"))

        assert len(promises) == 10

        before = time()

        for i, expected_wait in zip(range(10), [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]):
            r = await conn.getresponse()

            assert r.version == 20
            assert (await r.json())["url"].endswith(f"/delay/{expected_wait}")

            delay = round(time() - before, 2)

            assert expected_wait + 0.5 >= delay

        await conn.close()

    async def test_multiplexing_slowest_to_fastest(self) -> None:
        conn = AsyncHTTPSConnection(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver.new(),
        )

        promises = []

        for i in [5, 4, 3, 2, 1]:
            promises.append(await conn.request("GET", f"/delay/{i}"))
            promises.append(await conn.request("GET", f"/delay/{i}"))

        assert len(promises) == 10

        before = time()

        for expected_wait in [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]:
            r = await conn.getresponse()

            assert r.version == 20
            assert (await r.json())["url"].endswith(f"/delay/{expected_wait}")

            delay = round(time() - before, 2)

            assert expected_wait + 0.5 >= delay

        await conn.close()

    async def test_multiplexing_wait_for_promise(self) -> None:
        conn = AsyncHTTPSConnection(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver.new(),
        )

        promises = []

        for i in [5, 4, 3, 2, 1]:
            promises.append(await conn.request("GET", f"/delay/{i}"))
            promises.append(await conn.request("GET", f"/delay/{i}"))

        assert len(promises) == 10

        r = await conn.getresponse(promise=promises[2])  # the (first) 4 seconds delay

        assert r.version == 20
        assert (await r.json())["url"].endswith("/delay/4")

        # empty the promise queue
        for i in range(9):
            r = await conn.getresponse()
            assert r.version == 20

        assert len(conn._promises) == 0

    @pytest.mark.usefixtures("requires_http3")
    async def test_multiplexing_upgrade_h3(self) -> None:
        conn = AsyncHTTPSConnection(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver.new(),
        )

        for i in range(3):
            await conn.request("GET", "/get")

        for i in range(3):
            r = await conn.getresponse()
            assert r.version == 20

        for i in range(3):
            await conn.request("GET", "/get")

        for i in range(3):
            r = await conn.getresponse()
            assert r.version == 30
