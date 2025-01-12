from __future__ import annotations

import os
import platform
from json import JSONDecodeError, loads

import pytest

from urllib3 import AsyncHTTPSConnectionPool
from urllib3.backend._async.hface import _HAS_HTTP3_SUPPORT  # type: ignore

from .. import TraefikTestCase


@pytest.mark.asyncio
class TestStreamResponse(TraefikTestCase):
    @pytest.mark.parametrize(
        "amt",
        [
            None,
            1,
            3,
            5,
            16,
            64,
            1024,
            16544,
        ],
    )
    async def test_h2n3_stream(self, amt: int | None) -> None:
        async with AsyncHTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver.new(),
        ) as p:
            for i in range(3):
                resp = await p.request("GET", "/get", preload_content=False)

                assert resp.status == 200
                if _HAS_HTTP3_SUPPORT():
                    # colima is our only way to test HTTP/2 and HTTP/3 in GHA runners
                    # its known to have flaky behaviors. We can lose the connection easily...
                    # and our automatic downgrade to HTTP/2 makes the following assert
                    # problematic!
                    if (
                        os.environ.get("CI") is not None
                        and platform.system() == "Darwin"
                    ):
                        assert resp.version in {20, 30}
                    else:
                        assert resp.version == (20 if i == 0 else 30)
                else:
                    assert resp.version == 20

                chunks = []

                async for chunk in resp.stream(amt):
                    chunks.append(chunk)

                try:
                    payload_reconstructed = loads(b"".join(chunks))
                except JSONDecodeError as e:
                    print(e)
                    payload_reconstructed = None

                assert payload_reconstructed is not None, (
                    f"HTTP/{resp.version / 10} stream failure"
                )
                assert "args" in payload_reconstructed, (
                    f"HTTP/{resp.version / 10} stream failure"
                )

    async def test_read_zero(self) -> None:
        async with AsyncHTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=[self.test_async_resolver],
        ) as p:
            resp = await p.request("GET", "/get", preload_content=False)
            assert resp.status == 200

            assert await resp.read(0) == b""

            for i in range(5):
                assert len(await resp.read(1)) == 1

            assert await resp.read(0) == b""
            assert len(await resp.read()) > 0
