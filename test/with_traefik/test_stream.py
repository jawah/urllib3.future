from __future__ import annotations

import os
import platform
from json import JSONDecodeError, loads

import pytest

from urllib3 import HTTPSConnectionPool
from urllib3.backend.hface import _HAS_HTTP3_SUPPORT

from . import TraefikTestCase


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
    def test_h2n3_stream(self, amt: int | None) -> None:
        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=[self.test_resolver_raw],
        ) as p:
            for i in range(3):
                resp = p.request("GET", "/get", preload_content=False)

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

                for chunk in resp.stream(amt):
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

    def test_h2n3_stream_gzip_amt_negative_one(self) -> None:
        # Regression test for https://github.com/jawah/urllib3.future/issues/364
        # ``stream(amt=-1)`` over an HTTP/2 (or HTTP/3) gzip-encoded response
        # used to loop forever when the decoder still had unconsumed tail
        # bytes after the bomb-safe ``max_length`` cap was reached.
        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=[self.test_resolver_raw],
        ) as p:
            resp = p.request("GET", "/gzip", preload_content=False)
            assert resp.status == 200
            assert resp.headers.get("Content-Encoding", "").lower() == "gzip"

            chunks: list[bytes] = []
            for chunk in resp.stream(amt=-1):
                chunks.append(chunk)
                # Defensive: at the time of writing /gzip returns < 1 KiB
                # decoded; allow ample slack but never an infinite loop.
                assert len(chunks) < 256, (
                    f"stream(amt=-1) iterates too many times (HTTP/{resp.version / 10})"
                )

            try:
                payload = loads(b"".join(chunks))
            except JSONDecodeError as e:
                pytest.fail(
                    f"HTTP/{resp.version / 10} gzip stream(amt=-1) "
                    f"did not yield decodable JSON: {e}"
                )
            assert payload.get("gzipped") is True

    def test_read_zero(self) -> None:
        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
        ) as p:
            resp = p.request("GET", "/get", preload_content=False)
            assert resp.status == 200

            assert resp.read(0) == b""

            for i in range(5):
                assert len(resp.read(1)) == 1

            assert resp.read(0) == b""
            assert len(resp.read()) > 0
