from __future__ import annotations

import sys

import pytest

from urllib3 import ConnectionInfo, HttpVersion, PoolManager
from urllib3.exceptions import InsecureRequestWarning

from . import TraefikTestCase


class TestConnectionInfo(TraefikTestCase):
    def test_no_tls(self) -> None:
        p = PoolManager(ca_certs=self.ca_authority, resolver=self.test_resolver)

        conn_info: ConnectionInfo | None = None

        def on_post_connection(o: ConnectionInfo) -> None:
            nonlocal conn_info
            conn_info = o

        p.urlopen(
            method="GET", url=self.http_url, on_post_connection=on_post_connection
        )

        assert conn_info is not None
        assert conn_info.certificate_der is None
        assert conn_info.http_version == HttpVersion.h11
        assert conn_info.certificate_dict is None

        p.clear()

    def test_tls_on_tcp(self) -> None:
        p = PoolManager(ca_certs=self.ca_authority, resolver=self.test_resolver)

        conn_info: ConnectionInfo | None = None

        def on_post_connection(o: ConnectionInfo) -> None:
            nonlocal conn_info
            conn_info = o

        p.urlopen(
            method="GET", url=self.https_url, on_post_connection=on_post_connection
        )

        assert conn_info is not None
        assert conn_info.certificate_der is not None
        assert conn_info.http_version == HttpVersion.h2
        assert conn_info.tls_version is not None
        assert conn_info.cipher is not None

        p.clear()

    @pytest.mark.skipif(
        sys.version_info < (3, 10),
        reason="unsupported due missing API (3.10+)",
    )
    def test_tls_on_tcp_disabled_cert_reqs(self) -> None:
        p = PoolManager(
            ca_certs=self.ca_authority, cert_reqs=0, resolver=self.test_resolver
        )

        conn_info: ConnectionInfo | None = None

        def on_post_connection(o: ConnectionInfo) -> None:
            nonlocal conn_info
            conn_info = o

        with pytest.warns(InsecureRequestWarning):
            p.urlopen(
                method="GET", url=self.https_url, on_post_connection=on_post_connection
            )

        assert conn_info is not None
        assert conn_info.certificate_der is not None
        assert conn_info.certificate_dict is not None
        assert conn_info.http_version == HttpVersion.h2
        assert conn_info.tls_version is not None
        assert conn_info.cipher is not None

        p.clear()

    @pytest.mark.usefixtures("requires_http3")
    def test_tls_on_udp(self) -> None:
        p = PoolManager(
            preemptive_quic_cache={
                (self.host, self.https_port): (self.host, self.https_port)
            },
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
        )

        conn_info: ConnectionInfo | None = None

        def on_post_connection(o: ConnectionInfo) -> None:
            nonlocal conn_info
            conn_info = o

        p.urlopen(
            method="GET", url=self.https_url, on_post_connection=on_post_connection
        )

        assert conn_info is not None
        assert conn_info.certificate_der is not None
        assert conn_info.tls_version is not None
        assert conn_info.cipher is not None
        assert conn_info.http_version == HttpVersion.h3

        p.clear()
