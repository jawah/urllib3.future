from __future__ import annotations

import pytest

from urllib3 import HttpVersion
from urllib3._async.connection import AsyncHTTPSConnection
from urllib3.exceptions import ResponseNotReady

from .. import TraefikTestCase


@pytest.mark.asyncio
class TestConnection(TraefikTestCase):
    async def test_h3_probe_after_close(self) -> None:
        conn = AsyncHTTPSConnection(
            self.host, self.https_port, ca_certs=self.ca_authority
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

    async def test_getresponse_not_ready(self) -> None:
        conn = AsyncHTTPSConnection(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
        )

        await conn.close()

        with pytest.raises(ResponseNotReady):
            await conn.getresponse()

    async def test_quic_cache_capable(self) -> None:
        quic_cache_resumption: dict[tuple[str, int], tuple[str, int] | None] = {
            (self.host, self.https_port): ("", self.https_port)
        }

        conn = AsyncHTTPSConnection(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            preemptive_quic_cache=quic_cache_resumption,
        )

        await conn.request("GET", "/get")
        resp = await conn.getresponse()

        assert resp.status == 200
        assert resp.version == 30

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
        )

        await conn.request("GET", "/get")
        resp = await conn.getresponse()

        assert resp.status == 200
        assert resp.version == 20

    async def test_quic_cache_explicit_not_capable(self) -> None:
        quic_cache_resumption: dict[tuple[str, int], tuple[str, int] | None] = {
            (self.host, self.https_port): None
        }

        conn = AsyncHTTPSConnection(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            preemptive_quic_cache=quic_cache_resumption,
        )

        await conn.request("GET", "/get")
        resp = await conn.getresponse()

        assert resp.status == 200
        assert resp.version == 20

    async def test_quic_cache_implicit_not_capable(self) -> None:
        quic_cache_resumption: dict[tuple[str, int], tuple[str, int] | None] = dict()

        conn = AsyncHTTPSConnection(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            preemptive_quic_cache=quic_cache_resumption,
        )

        await conn.request("GET", "/get")
        resp = await conn.getresponse()

        assert resp.status == 200
        assert resp.version == 20

        assert len(quic_cache_resumption.keys()) == 1
        assert (self.host, self.https_port) in quic_cache_resumption
