from __future__ import annotations

import os
import socket

import pytest
import trustme

from urllib3 import HttpVersion, AsyncHTTPSConnectionPool
from urllib3._async.connection import AsyncHTTPSConnection
from urllib3.backend.hface import _HAS_HTTP3_SUPPORT
from urllib3.exceptions import ResponseNotReady, SSLError
from urllib3.util import create_urllib3_context

from .. import TraefikTestCase


@pytest.mark.asyncio
class TestConnection(TraefikTestCase):
    @pytest.mark.parametrize("failure", ["fingerprint", "untrusted-ca"])
    async def test_quic_certificate_rejection(self, failure: str) -> None:
        if not _HAS_HTTP3_SUPPORT():
            pytest.skip("HTTP/3 requires qh3")
        options = (
            {"assert_fingerprint": "00" * 32, "ca_certs": self.ca_authority}
            if failure == "fingerprint"
            else {"ca_cert_data": trustme.CA().cert_pem.bytes().decode()}
        )
        async with AsyncHTTPSConnectionPool(
            self.host,
            self.https_port,
            resolver=self.test_async_resolver,
            disabled_svn={HttpVersion.h11, HttpVersion.h2},
            timeout=5,
            retries=False,
            **options,
        ) as pool:
            with pytest.raises(SSLError):
                await pool.request("GET", "/get")

    @pytest.mark.parametrize(
        "version, expected_version",
        [(HttpVersion.h11, 11), (HttpVersion.h2, 20), (HttpVersion.h3, 30)],
    )
    @pytest.mark.parametrize("chunked", [False, True])
    async def test_streamed_request_reuses_connection(
        self, version: HttpVersion, expected_version: int, chunked: bool
    ) -> None:
        if version is HttpVersion.h3 and not _HAS_HTTP3_SUPPORT():
            pytest.skip("Test requires HTTP/3 support")

        conn = AsyncHTTPSConnection(
            self.host,
            self.https_port,
            timeout=5,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver.new(),
            disabled_svn=set(HttpVersion) - {version},
        )
        body = b"a streamed request body"
        headers = {"Host": self.alt_host, "Content-Type": "text/plain"}
        if not chunked:
            headers["Content-Length"] = str(len(body))
        try:
            await conn.connect()
            sock = conn.sock
            for _ in range(2):
                await conn.request(
                    "POST",
                    "/post",
                    body=iter((body[:5], body[5:])),
                    headers=headers,
                    chunked=chunked,
                )
                response = await conn.getresponse()
                assert response.status == 200
                assert response.version == expected_version
                payload = await response.json()
                assert payload["data"] == body.decode()
                assert payload["headers"]["Host"] == [self.alt_host]
                assert conn.sock is sock
        finally:
            await conn.close()

    @pytest.mark.usefixtures("requires_http3")
    async def test_h3_probe_after_close(self) -> None:
        conn = AsyncHTTPSConnection(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver.new(),
        )

        await conn.request("GET", "/get")

        resp = await conn.getresponse()

        assert resp.version == 20

        await conn.close()

        await conn.connect()

        await conn.request("GET", "/get")

        resp = await conn.getresponse()

        assert resp.version == 30

        await conn.close()

    async def test_h2_svn_conserved(self) -> None:
        conn = AsyncHTTPSConnection(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            disabled_svn={HttpVersion.h3},
            resolver=self.test_async_resolver.new(),
        )

        await conn.request("GET", "/get")

        resp = await conn.getresponse()

        assert resp.version == 20

        await conn.close()

        assert hasattr(conn, "_http_vsn") and conn._http_vsn == 20

        await conn.connect()

        await conn.request("GET", "/get")

        resp = await conn.getresponse()

        assert resp.version == 20

        await conn.close()

    async def test_getresponse_not_ready(self) -> None:
        conn = AsyncHTTPSConnection(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver.new(),
        )

        await conn.close()

        with pytest.raises(ResponseNotReady):
            await conn.getresponse()

    @pytest.mark.usefixtures("requires_http3")
    async def test_quic_cache_capable(self) -> None:
        quic_cache_resumption: dict[tuple[str, int], tuple[str, int] | None] = {
            (self.host, self.https_port): ("", self.https_port)
        }

        conn = AsyncHTTPSConnection(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            preemptive_quic_cache=quic_cache_resumption,
            resolver=self.test_async_resolver.new(),
        )

        await conn.request("GET", "/get")
        resp = await conn.getresponse()

        assert resp.status == 200
        assert resp.version == 30

        await conn.close()

    @pytest.mark.usefixtures("requires_http3")
    async def test_quic_cache_capable_but_disabled(self) -> None:
        quic_cache_resumption: dict[tuple[str, int], tuple[str, int] | None] = {
            (self.host, self.https_port): ("", self.https_port)
        }

        conn = AsyncHTTPSConnection(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            preemptive_quic_cache=quic_cache_resumption,
            disabled_svn={HttpVersion.h3},
            resolver=self.test_async_resolver.new(),
        )

        await conn.request("GET", "/get")
        resp = await conn.getresponse()

        assert resp.status == 200
        assert resp.version == 20

        await conn.close()

    @pytest.mark.usefixtures("requires_http3")
    async def test_quic_cache_explicit_not_capable(self) -> None:
        quic_cache_resumption: dict[tuple[str, int], tuple[str, int] | None] = {
            (self.host, self.https_port): None
        }

        conn = AsyncHTTPSConnection(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            preemptive_quic_cache=quic_cache_resumption,
            resolver=self.test_async_resolver.new(),
        )

        await conn.request("GET", "/get")
        resp = await conn.getresponse()

        assert resp.status == 200
        assert resp.version == 20

        await conn.close()

    @pytest.mark.usefixtures("requires_http3")
    async def test_quic_cache_implicit_not_capable(self) -> None:
        quic_cache_resumption: dict[tuple[str, int], tuple[str, int] | None] = dict()

        conn = AsyncHTTPSConnection(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            preemptive_quic_cache=quic_cache_resumption,
            resolver=self.test_async_resolver.new(),
        )

        await conn.request("GET", "/get")
        resp = await conn.getresponse()

        assert resp.status == 200
        assert resp.version == 20

        assert len(quic_cache_resumption.keys()) == 1
        assert (self.host, self.https_port) in quic_cache_resumption

        await conn.close()

    @pytest.mark.usefixtures("requires_http3")
    async def test_quic_extract_ssl_ctx_ca_root(self) -> None:
        quic_cache_resumption: dict[tuple[str, int], tuple[str, int] | None] = {
            (self.host, self.https_port): ("", self.https_port)
        }

        ctx = create_urllib3_context()
        ctx.load_verify_locations(cafile=self.ca_authority)

        conn = AsyncHTTPSConnection(
            self.host,
            self.https_port,
            ssl_context=ctx,
            preemptive_quic_cache=quic_cache_resumption,
            resolver=self.test_async_resolver.new(),
        )

        await conn.request("GET", "/get")
        resp = await conn.getresponse()

        assert conn._AsyncHfaceBackend__custom_tls_settings is not None  # type: ignore
        detect_ctx_fallback = conn._AsyncHfaceBackend__custom_tls_settings.cadata  # type: ignore

        assert detect_ctx_fallback is not None
        assert isinstance(detect_ctx_fallback, bytes)
        assert self.ca_authority is not None

        with open(self.ca_authority, "rb") as fp:
            assert fp.read() in detect_ctx_fallback

        assert resp.status == 200
        assert resp.version == 30

        await conn.close()

    @pytest.mark.xfail(
        os.environ.get("CI") is not None, reason="Flaky in CI", strict=False
    )
    async def test_fast_reuse_outgoing_port(self) -> None:
        def _get_free_port(host: str) -> int:
            s = socket.socket()
            s.bind((host, 0))
            port = s.getsockname()[1]
            s.close()
            return port  # type: ignore[no-any-return]

        _tba_port = _get_free_port("localhost")

        for _ in range(4):
            conn = AsyncHTTPSConnection(
                self.host,
                self.https_port,
                ca_certs=self.ca_authority,
                resolver=self.test_async_resolver.new(),
                source_address=("0.0.0.0", _tba_port),
            )

            await conn.connect()

            await conn.request("GET", "/get")
            resp = await conn.getresponse()

            assert resp.status == 200

            await conn.close()
