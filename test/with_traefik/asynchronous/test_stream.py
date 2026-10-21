from __future__ import annotations

import os
import platform
from time import monotonic
from typing import Any, Callable
from json import JSONDecodeError, loads

import pytest

from urllib3 import AsyncHTTPSConnectionPool, HttpVersion
from urllib3._async.connection import AsyncHTTPSConnection
from urllib3.backend._async.hface import _HAS_HTTP3_SUPPORT  # type: ignore

from urllib3.backend._async import AsyncLowLevelResponse

from .. import TraefikTestCase


@pytest.mark.asyncio
class TestStreamResponse(TraefikTestCase):
    async def test_failed_stream_reset_closes_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import errno

        async with AsyncHTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver.new(),
            disabled_svn={HttpVersion.h11, HttpVersion.h3},
            timeout=5,
            retries=False,
            background_watch_delay=None,
        ) as pool:
            response = await pool.request(
                "GET", "/drip?duration=10&numbytes=2&delay=0", preload_content=False
            )
            assert response.version == 20
            assert len(await response.read(1)) == 1
            conn = response.connection
            assert conn is not None and conn.sock is not None
            sock = conn.sock
            failed_writes = []

            async def fail_sendall(data: bytes) -> None:
                failed_writes.append(data)
                raise OSError(errno.EPIPE, "stream reset write failed")

            # Only the write boundary fails; negotiation and the response are real.
            with monkeypatch.context() as patch:
                patch.setattr(sock, "sendall", fail_sendall)
                await response.close()
            assert failed_writes
            assert response.closed and conn.is_closed
            following = await pool.request("GET", "/bytes/7")
            assert len(await following.data) == 7

    @pytest.mark.parametrize("version", [HttpVersion.h2, HttpVersion.h3])
    @pytest.mark.parametrize("body_state", ["queued", "buffered", "unfinished"])
    @pytest.mark.parametrize("close_owner", [False, True])
    async def test_close_response_preserves_neighbour(
        self,
        version: HttpVersion,
        body_state: str,
        close_owner: bool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        if version is HttpVersion.h3 and not _HAS_HTTP3_SUPPORT():
            pytest.skip("HTTP/3 requires qh3")
        async with AsyncHTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver.new(),
            disabled_svn=set(HttpVersion) - {version},
            timeout=5,
            retries=False,
            background_watch_delay=None,
        ) as pool:
            path = (
                "/drip?duration=10&numbytes=2&delay=0"
                if body_state == "unfinished"
                else "/bytes/8192"
            )
            first = await pool.request("GET", path, preload_content=False)
            assert first.status == 200, await first.data
            conn = first.connection
            assert isinstance(conn, AsyncHTTPSConnection) and conn.sock is not None
            sock, protocol = conn.sock, conn._protocol
            assert protocol is not None
            assert isinstance(first._fp, AsyncLowLevelResponse)
            assert first._fp.from_promise is not None
            stream_id = first._fp.from_promise.stream_id
            wire_calls: list[tuple[str, int, int]] = []
            transport = getattr(protocol, "_connection", None) or getattr(
                protocol, "_quic"
            )

            def record(method: str) -> Callable[..., None]:
                original = getattr(type(transport), method)

                def call(instance: Any, stream_id: int, error_code: int = 0) -> None:
                    if instance is transport:
                        wire_calls.append((method, stream_id, error_code))
                    original(instance, stream_id, error_code)

                return call

            for method in (
                ("reset_stream",)
                if version is HttpVersion.h2
                else ("stop_stream", "reset_stream")
            ):
                monkeypatch.setattr(type(transport), method, record(method))
            if body_state == "buffered":
                assert len(await first.read(8191)) == 8191
                # Body data and END_STREAM can arrive separately. Keep the last
                # byte unread, but observe peer completion before expecting no reset.
                police = pool.pool
                assert police is not None
                deadline = monotonic() + 5
                while stream_id in getattr(protocol, "_open_streams"):
                    assert monotonic() < deadline, "Peer did not finish the stream"
                    assert not protocol.has_expired()
                    async with police.borrow(first):
                        await conn.peek_and_react(expect_frame=True)
            elif body_state == "unfinished":
                assert len(await first.read(1)) == 1
            second = await pool.request("GET", "/bytes/16", preload_content=False)
            assert second.connection is conn
            police = pool.pool
            assert police is not None
            if close_owner:
                await pool.close()
            await first.close()
            await first.close()
            assert first.closed
            if body_state == "buffered":
                assert wire_calls == []
            elif body_state == "unfinished":
                expected = (
                    ("reset_stream", stream_id, 8)
                    if version is HttpVersion.h2
                    else ("stop_stream", stream_id, 0x10C)
                )
                assert wire_calls == [expected]
            assert conn.sock is sock and sock.fileno() >= 0
            assert stream_id not in conn._pending_responses
            assert not protocol.has_pending_event(stream_id=stream_id)
            assert len(await second.read()) == 16
            assert not protocol.has_pending_event(stream_id=stream_id)
            if close_owner:
                assert sock.fileno() == -1
                assert police.rsize() == 0
            else:
                assert protocol.is_idle()
                third = await pool.request("GET", "/bytes/7", preload_content=False)
                assert third.connection is conn
                assert len(await third.read()) == 7

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

    async def test_h2n3_stream_gzip_amt_negative_one(self) -> None:
        # Async mirror of the regression test for issue #364.
        async with AsyncHTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver.new(),
        ) as p:
            resp = await p.request("GET", "/gzip", preload_content=False)
            assert resp.status == 200
            assert resp.headers.get("Content-Encoding", "").lower() == "gzip"

            chunks: list[bytes] = []
            async for chunk in resp.stream(amt=-1):
                chunks.append(chunk)
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

    @pytest.mark.parametrize("target_http", [11, 20, 30])
    async def test_sse_stream_amt_yields_per_event(self, target_http: int) -> None:
        # Async mirror of the regression test for
        # https://github.com/jawah/urllib3.future/issues/379
        # ``stream(amt)`` against a live stream (here SSE) must yield data as
        # it arrives, even when fewer than ``amt`` bytes are available, instead
        # of blocking until ``amt`` bytes accumulate or the stream ends.
        if target_http == 30 and _HAS_HTTP3_SUPPORT() is False:
            pytest.skip("Test requires http3 support")

        disabled_svn = {
            11: {HttpVersion.h2, HttpVersion.h3},
            20: {HttpVersion.h11, HttpVersion.h3},
            30: {HttpVersion.h11, HttpVersion.h2},
        }[target_http]

        async with AsyncHTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver.new(),
            disabled_svn=disabled_svn,
            retries=False,
        ) as p:
            resp = await p.request(
                "GET", "/sse?delay=1s&count=5", preload_content=False
            )

            assert resp.status == 200
            assert resp.version == target_http

            chunks = []

            async for chunk in resp.stream(2**16):
                chunks.append(chunk)

            payload = b"".join(chunks)
            assert payload.count(b"event:") == 5

            # the whole stream is far smaller than ``amt``: the broken
            # behavior was a single monolithic yield delivered only once
            # the server closed the stream. Per-arrival delivery must give
            # us the first event alone in the first yield.
            assert chunks[0].count(b"event:") == 1
            assert len(chunks) >= 5

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
