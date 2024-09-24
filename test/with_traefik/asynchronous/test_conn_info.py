from __future__ import annotations

import pytest

from urllib3 import AsyncPoolManager, ConnectionInfo, HttpVersion

from .. import TraefikTestCase


@pytest.mark.asyncio
class TestConnectionInfo(TraefikTestCase):
    async def test_no_tls(self) -> None:
        p = AsyncPoolManager(
            ca_certs=self.ca_authority, resolver=self.test_async_resolver
        )

        conn_info: ConnectionInfo | None = None

        async def on_post_connection(o: ConnectionInfo) -> None:
            nonlocal conn_info
            conn_info = o

        await p.urlopen(
            method="GET", url=self.http_url, on_post_connection=on_post_connection
        )

        assert conn_info is not None
        assert conn_info.certificate_der is None
        assert conn_info.http_version == HttpVersion.h11
        assert conn_info.certificate_dict is None

    async def test_tls_on_tcp(self) -> None:
        p = AsyncPoolManager(
            ca_certs=self.ca_authority, resolver=self.test_async_resolver
        )

        conn_info: ConnectionInfo | None = None

        async def on_post_connection(o: ConnectionInfo) -> None:
            nonlocal conn_info
            conn_info = o

        await p.urlopen(
            method="GET", url=self.https_url, on_post_connection=on_post_connection
        )

        assert conn_info is not None
        assert conn_info.certificate_der is not None
        assert conn_info.http_version == HttpVersion.h2
        assert conn_info.tls_version is not None
        assert conn_info.cipher is not None

    @pytest.mark.usefixtures("requires_http3")
    async def test_tls_on_udp(self) -> None:
        p = AsyncPoolManager(
            preemptive_quic_cache={
                (self.host, self.https_port): (self.host, self.https_port)
            },
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver,
        )

        conn_info: ConnectionInfo | None = None

        async def on_post_connection(o: ConnectionInfo) -> None:
            nonlocal conn_info
            conn_info = o

        await p.urlopen(
            method="GET", url=self.https_url, on_post_connection=on_post_connection
        )

        assert conn_info is not None
        assert conn_info.certificate_der is not None
        assert conn_info.tls_version is not None
        assert conn_info.cipher is not None
        assert conn_info.http_version == HttpVersion.h3
