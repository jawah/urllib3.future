from __future__ import annotations

import pytest

from urllib3 import HTTPConnectionPool, HTTPSConnectionPool, HttpVersion, PoolManager
from urllib3.backend.hface import _HAS_HTTP3_SUPPORT
from urllib3.connection import HTTPSConnection

from . import TraefikTestCase


class TestSvnCapability(TraefikTestCase):
    def test_h11_only(self) -> None:
        with HTTPConnectionPool(
            self.host, self.http_port, resolver=self.test_resolver
        ) as p:
            resp = p.request("GET", "/get")
            assert resp.version == 11

    def test_h11_no_upgrade(self) -> None:
        with HTTPConnectionPool(
            host="httpbin.local", port=8888, resolver=self.test_resolver
        ) as p:
            for i in range(3):
                resp = p.request("GET", "/get")

                assert resp.version == 11

    def test_alpn_h2_default(self) -> None:
        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
        ) as p:
            resp = p.request("GET", "/get")

            assert resp.version == 20

    @pytest.mark.usefixtures("requires_http3")
    def test_upgrade_h3(self) -> None:
        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            timeout=5,
            retries=0,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
        ) as p:
            for i in range(3):
                resp = p.request("GET", "/get")
                assert resp.version == (20 if i == 0 else 30)

    @pytest.mark.usefixtures("requires_http3")
    def test_explicitly_disable_h3(self) -> None:
        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            timeout=5,
            retries=0,
            ca_certs=self.ca_authority,
            disabled_svn={HttpVersion.h3},
            resolver=self.test_resolver,
        ) as p:
            for i in range(3):
                resp = p.request("GET", "/get")
                assert resp.version == 20

    def test_explicitly_disable_h2(self) -> None:
        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            timeout=5,
            retries=0,
            ca_certs=self.ca_authority,
            disabled_svn={HttpVersion.h2},
            resolver=self.test_resolver,
        ) as p:
            for i in range(3):
                resp = p.request("GET", "/get")
                if _HAS_HTTP3_SUPPORT():
                    assert resp.version == (11 if i == 0 else 30)
                else:
                    assert resp.version == 11

    def test_can_disable_h11(self) -> None:
        p = HTTPSConnectionPool(
            self.host,
            self.https_port,
            timeout=5,
            retries=0,
            ca_certs=self.ca_authority,
            disabled_svn={HttpVersion.h11},
            resolver=self.test_resolver,
        )

        r = p.request("GET", "/get")

        assert r.status == 200
        assert r.version == 20

    def test_cannot_disable_everything(self) -> None:
        with pytest.raises(RuntimeError):
            p = HTTPSConnectionPool(
                self.host,
                self.https_port,
                timeout=5,
                retries=0,
                ca_certs=self.ca_authority,
                disabled_svn={HttpVersion.h11, HttpVersion.h2, HttpVersion.h3},
                resolver=self.test_resolver,
            )

            p.request("GET", "/get")

        with pytest.raises(RuntimeError):
            p = HTTPConnectionPool(  # type: ignore[assignment]
                self.host,
                self.http_port,
                timeout=5,
                retries=0,
                disabled_svn={HttpVersion.h11, HttpVersion.h2},
                resolver=self.test_resolver,
            )

            p.request("GET", "/get")

    def test_cant_upgrade_h3(self) -> None:
        with HTTPSConnectionPool(
            self.host,
            self.https_alt_port,
            timeout=5,
            retries=False,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
        ) as p:
            for i in range(3):
                resp = p.request(
                    "GET",
                    "/response-headers?Alt-Svc=h3-25%3D%22%3A443%22%3B%20ma%3D3600%2C%20h2%3D%22%3A443%22%3B%20ma%3D3600",
                )

                assert resp.version == 20
                assert "Alt-Svc" in resp.headers
                assert (
                    resp.headers.get("Alt-Svc")
                    == 'h3-25=":443"; ma=3600, h2=":443"; ma=3600'
                )

    def test_illegal_upgrade_h3(self) -> None:
        with HTTPSConnectionPool(
            self.host,
            self.https_alt_port,
            timeout=5,
            retries=False,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
        ) as p:
            for i in range(3):
                resp = p.request(
                    "GET",
                    "/response-headers?Alt-Svc=h3-25%3D%22%3A443%22%3B%20ma%3D3600%2C%20h3%3D%22evil.httpbin.local%3A443%22%3B%20ma%3D3600",
                )

                assert resp.version == 20
                assert "Alt-Svc" in resp.headers
                assert (
                    resp.headers.get("Alt-Svc")
                    == 'h3-25=":443"; ma=3600, h3="evil.httpbin.local:443"; ma=3600'
                )

    @pytest.mark.usefixtures("requires_http3")
    def test_other_port_upgrade_h3(self) -> None:
        with HTTPSConnectionPool(
            self.host,
            self.https_alt_port,
            timeout=5,
            retries=False,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
        ) as p:
            for i in range(2):
                resp = p.request(
                    "GET",
                    f"/response-headers?Alt-Svc=h3-25%3D%22%3A443%22%3B%20ma%3D3600%2C%20h3%3D%22%3A{self.https_port}%22%3B%20ma%3D3600",
                )

                assert resp.version == (20 if i == 0 else 30)
                assert "Alt-Svc" in resp.headers
                assert (
                    resp.headers.get("Alt-Svc")
                    == f'h3-25=":443"; ma=3600, h3=":{self.https_port}"; ma=3600'
                )

    @pytest.mark.usefixtures("requires_http3")
    def test_misleading_upgrade_h3(self) -> None:
        dumb_cache: dict[tuple[str, int], tuple[str, int] | None] = dict()

        with HTTPSConnectionPool(
            self.host,
            self.https_alt_port,
            retries=False,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
            preemptive_quic_cache=dumb_cache,
        ) as p:
            for i in range(2):
                resp = p.request(
                    "GET",
                    "/response-headers?Alt-Svc=h3-25%3D%22%3A443%22%3B%20ma%3D3600%2C%20h3%3D%22%3A6547%22%3B%20ma%3D3600",
                )

                assert resp.version == 20
                assert "Alt-Svc" in resp.headers
                assert (
                    resp.headers.get("Alt-Svc")
                    == 'h3-25=":443"; ma=3600, h3=":6547"; ma=3600'
                )

        # this asserts tell us that we correctly unset the entry from "preemptive_quic_cache"
        # because the alt-svc entry was misleading, thus invalid.
        assert len(dumb_cache) == 0

    def test_invalid_alt_svc_h3_upgrade(self) -> None:
        with HTTPSConnectionPool(
            self.host,
            self.https_alt_port,
            timeout=5,
            retries=False,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
        ) as p:
            for i in range(2):
                resp = p.request(
                    "GET",
                    "/response-headers?Alt-Svc=h3-25%3D%22%3A443%22%3B%20ma%3D3600%2C%20h3%3D%22%3Aabc%22%3B%20ma%3D3600",
                )

                assert resp.version == 20
                assert "Alt-Svc" in resp.headers
                assert (
                    resp.headers.get("Alt-Svc")
                    == 'h3-25=":443"; ma=3600, h3=":abc"; ma=3600'
                )

    def test_drop_h3_upgrade(self) -> None:
        conn = HTTPSConnection(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver.new(),
        )

        conn.request("GET", "/get")
        resp = conn.getresponse()

        assert resp.version == 20
        assert resp.status == 200
        conn.close()

        conn.host = self.alt_host
        conn.port = self.https_alt_port

        conn.connect()
        conn.request("GET", "/get")
        resp = conn.getresponse()

        assert resp.version == 20
        assert resp.status == 200

    @pytest.mark.usefixtures("requires_http3")
    def test_drop_post_established_h3(self) -> None:
        conn = HTTPSConnection(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver.new(),
        )

        conn.request("GET", "/get")
        resp = conn.getresponse()

        assert resp.version == 20
        assert resp.status == 200

        conn.request("GET", "/get")
        resp = conn.getresponse()

        assert resp.version == 30
        assert resp.status == 200

        conn.close()

        conn.host = self.alt_host
        conn.port = self.https_alt_port

        conn.connect()
        conn.request("GET", "/get")
        resp = conn.getresponse()

        assert resp.version == 20
        assert resp.status == 200

    @pytest.mark.usefixtures("requires_http3")
    def test_pool_manager_quic_cache(self) -> None:
        dumb_cache: dict[tuple[str, int], tuple[str, int] | None] = dict()
        pm = PoolManager(
            ca_certs=self.ca_authority,
            preemptive_quic_cache=dumb_cache,
            resolver=self.test_resolver,
        )

        conn = pm.connection_from_url(self.https_url)

        resp = conn.urlopen("GET", "/get")

        assert resp.status == 200
        assert resp.version == 20

        assert len(dumb_cache.keys()) == 1

        conn.close()

        pm = PoolManager(
            ca_certs=self.ca_authority,
            preemptive_quic_cache=dumb_cache,
            resolver=self.test_resolver,
        )
        conn = pm.connection_from_url(self.https_url)

        resp = conn.urlopen("GET", "/get")

        assert resp.status == 200
        assert resp.version == 30

        assert len(dumb_cache.keys()) == 1

    def test_can_upgrade_h2c_via_altsvc(self) -> None:
        with HTTPConnectionPool(
            self.host,
            self.http_alt_port,
            timeout=5,
            retries=False,
            resolver=self.test_resolver,
        ) as p:
            for i in range(3):
                resp = p.request(
                    "GET",
                    f"/response-headers?Alt-Svc=h2c%3D%22%3A{self.http_alt_port}%22",
                )

                assert resp.version == 11 if i == 0 else 20

                assert "Alt-Svc" in resp.headers
                assert resp.headers.get("Alt-Svc") == f'h2c=":{self.http_alt_port}"'
