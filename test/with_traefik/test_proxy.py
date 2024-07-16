from __future__ import annotations

import pytest

from dummyserver.server import DEFAULT_CA
from urllib3 import ConnectionInfo, HttpVersion, proxy_from_url
from urllib3.util.ssl_ import _SSLContextCache

from . import TraefikWithProxyTestCase


class TestProxyToTraefik(TraefikWithProxyTestCase):
    @classmethod
    def setup_class(cls) -> None:
        super().setup_class()

        cls.proxy_url = f"http://{cls.proxy_host}:{int(cls.proxy_port)}"
        cls.https_proxy_url = f"https://{cls.proxy_host}:{int(cls.https_proxy_port)}"

        if cls.ca_authority:
            with open(cls.ca_authority, "rb") as fp:
                trustme_traefik_ca = fp.read()
        else:
            trustme_traefik_ca = b""

        with open(DEFAULT_CA, "rb") as fp:
            trustme_ca = fp.read()

        if trustme_traefik_ca not in trustme_ca:
            with open(DEFAULT_CA, "wb") as fp:
                fp.write(trustme_ca)
                fp.write(trustme_traefik_ca)

        _SSLContextCache.clear()

    @classmethod
    def teardown_class(cls) -> None:
        super().teardown_class()

    @pytest.mark.parametrize(
        "proxy_url, destination_host, expected_max_svn, disabled_svn",
        [
            (
                "https_proxy_url",
                "http_url",
                HttpVersion.h11,
                None,
            ),
            (
                "https_proxy_url",
                "https_url",
                HttpVersion.h2,
                None,
            ),
            (
                "proxy_url",
                "http_url",
                HttpVersion.h11,
                None,
            ),
            (
                "proxy_url",
                "https_url",
                HttpVersion.h2,
                None,
            ),
            (
                "proxy_url",
                "https_url",
                HttpVersion.h11,
                HttpVersion.h2,
            ),
            (
                "https_proxy_url",
                "https_url",
                HttpVersion.h11,
                HttpVersion.h2,
            ),
        ],
    )
    def test_simple_proxy_get(
        self,
        proxy_url: str,
        destination_host: str,
        expected_max_svn: HttpVersion,
        disabled_svn: HttpVersion | None,
    ) -> None:
        with proxy_from_url(
            getattr(self, proxy_url),
            ca_certs=DEFAULT_CA,
            disabled_svn={disabled_svn},
            resolver=self.test_resolver,
        ) as http:
            svn_history = []

            for i in range(3):
                conn_info: ConnectionInfo | None = None

                def on_post_connection(o: ConnectionInfo) -> None:
                    nonlocal conn_info
                    conn_info = o

                resp = http.urlopen(
                    "GET",
                    f"{getattr(self, destination_host)}/get",
                    retries=False,
                    on_post_connection=on_post_connection,
                )

                assert resp.status == 200
                assert conn_info is not None
                svn_history.append(resp.version)

            assert svn_history[-1] == int(
                expected_max_svn.value.split("/")[-1].replace(".", "")
            )
