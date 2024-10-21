from __future__ import annotations

import socket
from time import sleep

import pytest

from urllib3 import HTTPConnectionPool, HTTPHeaderDict, HTTPSConnectionPool, HttpVersion
from urllib3._constant import MINIMAL_BACKGROUND_WATCH_WINDOW
from urllib3.backend.hface import _HAS_HTTP3_SUPPORT
from urllib3.exceptions import InsecureRequestWarning, ProtocolError
from urllib3.util import parse_url
from urllib3.util.request import SKIP_HEADER

from . import TraefikTestCase


class TestProtocolLevel(TraefikTestCase):
    def test_forbid_request_without_authority(self) -> None:
        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
        ) as p:
            with pytest.raises(
                ProtocolError,
                match="do not support emitting HTTP requests without the `Host` header",
            ):
                p.request(
                    "GET",
                    f"{self.https_url}/get",
                    headers={"Host": SKIP_HEADER},
                    retries=False,
                )

    @pytest.mark.parametrize(
        "headers",
        [
            [(f"x-urllib3-{p}", str(p)) for p in range(8)],
            [(f"x-urllib3-{p}", str(p)) for p in range(8)]
            + [(f"x-urllib3-{p}", str(p)) for p in range(16)],
            [("x-www-not-standard", "hello!world!")],
        ],
    )
    def test_headers(self, headers: list[tuple[str, str]]) -> None:
        dict_headers = dict(headers)

        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
        ) as p:
            resp = p.request(
                "GET",
                f"{self.https_url}/headers",
                headers=dict_headers,
                retries=False,
            )

            assert resp.status == 200

            temoin = HTTPHeaderDict(dict_headers)
            payload = resp.json()

            seen = []

            for key, value in payload["headers"].items():
                if key in temoin:
                    seen.append(key)
                    assert temoin.get(key) in value

            assert len(seen) == len(dict_headers.keys())

    def test_override_authority_via_host_header(self) -> None:
        assert self.https_url is not None

        parsed_url = parse_url(self.https_url)
        assert parsed_url.host is not None

        resolver = self.test_resolver.new()

        records = resolver.getaddrinfo(
            parsed_url.host,
            parsed_url.port,
            socket.AF_INET,
            socket.SOCK_STREAM,
        )

        target_ip = records[0][-1][0]

        with pytest.warns(InsecureRequestWarning):
            with HTTPSConnectionPool(
                target_ip, self.https_port, ca_certs=self.ca_authority, cert_reqs=0
            ) as p:
                resp = p.request(
                    "GET",
                    f"{self.https_url.replace(parsed_url.host, target_ip)}/get",
                    headers={"host": parsed_url.host},
                    retries=False,
                )

                assert resp.status == 200
                assert resp.version == 20

                resp = p.request(
                    "GET",
                    f"{self.https_url.replace(parsed_url.host, target_ip)}/get",
                    headers={"host": parsed_url.host},
                    retries=False,
                )

                assert resp.status == 200
                assert resp.version == 30 if _HAS_HTTP3_SUPPORT() else 20

    def test_http2_with_prior_knowledge(self) -> None:
        with HTTPConnectionPool(
            self.host,
            self.http_port,
            disabled_svn={HttpVersion.h11},
            resolver=self.test_resolver,
        ) as p:
            resp = p.request(
                "GET",
                f"{self.http_url}/get",
                retries=False,
            )

            assert resp.status == 200
            assert resp.version == 20

    @pytest.mark.parametrize(
        "expected_trailers",
        (
            {"test-trailer-1": "v1"},
            {"test-trailer-1": "v1", "foobar": "baz", "x-proto-winner": "woops"},
            {"hello": "world", "this": "shall work", "every": "single time!"},
            {},
        ),
    )
    @pytest.mark.parametrize(
        "disabled_svn",
        (
            {HttpVersion.h2, HttpVersion.h3},  # Force HTTP/1
            {HttpVersion.h11, HttpVersion.h3},  # ...   HTTP/2
            {HttpVersion.h11, HttpVersion.h2},  # ...   HTTP/3
        ),
    )
    def test_http_trailers(
        self, expected_trailers: dict[str, str], disabled_svn: set[HttpVersion]
    ) -> None:
        if HttpVersion.h11 not in disabled_svn:
            expected_http_version = 11
        elif HttpVersion.h2 not in disabled_svn:
            expected_http_version = 20
        elif HttpVersion.h3 not in disabled_svn:
            expected_http_version = 30
        else:
            assert False, "unable to asses expected protocol"

        if _HAS_HTTP3_SUPPORT() is False and expected_http_version == 30:
            pytest.skip("Test requires http3")

        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
            disabled_svn=disabled_svn,
        ) as p:
            resp = p.request_encode_url(
                "GET",
                f"{self.https_url}/trailers",
                fields=expected_trailers,
                retries=False,
            )

            assert resp.status == 200
            assert resp.version == expected_http_version

            if expected_trailers:
                assert resp.trailers is not None

                for k, v in expected_trailers.items():
                    assert k in resp.trailers
                    assert resp.trailers[k] == v
            else:
                assert resp.trailers is None

    @pytest.mark.parametrize("background_watch_delay", [2.0, 0.5, None, 0.0])
    @pytest.mark.parametrize(
        "sleep_delay",
        [
            2.5,
            0.8,
        ],
    )
    @pytest.mark.parametrize(
        "disabled_svn",
        [
            {
                HttpVersion.h11,
                HttpVersion.h2,
            },
            {
                HttpVersion.h11,
                HttpVersion.h3,
            },
            {
                HttpVersion.h2,
                HttpVersion.h3,
            },
        ],
    )
    def test_discrete_watcher_https(
        self,
        background_watch_delay: float | None,
        sleep_delay: float,
        disabled_svn: set[HttpVersion],
    ) -> None:
        if HttpVersion.h3 not in disabled_svn and _HAS_HTTP3_SUPPORT() is False:
            pytest.skip("Test requires http3")

        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
            background_watch_delay=background_watch_delay,
            disabled_svn=disabled_svn,
        ) as p:
            if (
                background_watch_delay is not None
                and background_watch_delay >= MINIMAL_BACKGROUND_WATCH_WINDOW
            ):
                assert p._background_monitoring is not None
                assert p._background_monitoring.is_alive()
            else:
                assert p._background_monitoring is None

            resp = p.urlopen("GET", f"{self.https_url}/get")

            assert resp.status == 200

            if (
                background_watch_delay is not None
                and background_watch_delay >= MINIMAL_BACKGROUND_WATCH_WINDOW
            ):
                assert p._background_monitoring is not None
                assert p._background_monitoring.is_alive()

            sleep(sleep_delay)

            if (
                background_watch_delay is not None
                and background_watch_delay >= MINIMAL_BACKGROUND_WATCH_WINDOW
            ):
                assert p._background_monitoring is not None
                assert p._background_monitoring.is_alive()

            resp = p.urlopen("GET", f"{self.https_url}/get")
            if HttpVersion.h3 in disabled_svn:
                assert resp.status == 200
            else:
                # quicgo potential issue?
                assert resp.status in {200, 502}

            if (
                background_watch_delay is not None
                and background_watch_delay >= MINIMAL_BACKGROUND_WATCH_WINDOW
            ):
                assert p._background_monitoring is not None
                assert p._background_monitoring.is_alive()

        assert p._background_monitoring is None

    @pytest.mark.parametrize("background_watch_delay", [2.0, 0.5, None, 0.0])
    @pytest.mark.parametrize(
        "sleep_delay",
        [
            2.5,
            0.5,
        ],
    )
    @pytest.mark.parametrize(
        "disabled_svn",
        [
            {
                HttpVersion.h11,
            },
            {
                HttpVersion.h2,
            },
        ],
    )
    def test_discrete_watcher_http(
        self,
        background_watch_delay: float | None,
        sleep_delay: float,
        disabled_svn: set[HttpVersion],
    ) -> None:
        with HTTPConnectionPool(
            self.host,
            self.http_port,
            resolver=self.test_resolver,
            background_watch_delay=background_watch_delay,
            disabled_svn=disabled_svn,
        ) as p:
            if (
                background_watch_delay is not None
                and background_watch_delay >= MINIMAL_BACKGROUND_WATCH_WINDOW
            ):
                assert p._background_monitoring is not None
                assert p._background_monitoring.is_alive()
            else:
                assert p._background_monitoring is None

            resp = p.urlopen("GET", f"{self.http_url}/get")

            assert resp.status == 200

            if (
                background_watch_delay is not None
                and background_watch_delay >= MINIMAL_BACKGROUND_WATCH_WINDOW
            ):
                assert p._background_monitoring is not None
                assert p._background_monitoring.is_alive()

            sleep(sleep_delay)

            if (
                background_watch_delay is not None
                and background_watch_delay >= MINIMAL_BACKGROUND_WATCH_WINDOW
            ):
                assert p._background_monitoring is not None
                assert p._background_monitoring.is_alive()

            resp = p.urlopen("GET", f"{self.http_url}/get")
            assert resp.status == 200

            if (
                background_watch_delay is not None
                and background_watch_delay >= MINIMAL_BACKGROUND_WATCH_WINDOW
            ):
                assert p._background_monitoring is not None
                assert p._background_monitoring.is_alive()

        assert p._background_monitoring is None

    @pytest.mark.parametrize(
        "disabled_svn",
        [
            {
                HttpVersion.h11,
                HttpVersion.h2,
            },
            {
                HttpVersion.h11,
                HttpVersion.h3,
            },
            {
                HttpVersion.h2,
                HttpVersion.h3,
            },
        ],
    )
    def test_automated_ping(self, disabled_svn: set[HttpVersion]) -> None:
        if HttpVersion.h3 not in disabled_svn and _HAS_HTTP3_SUPPORT() is False:
            pytest.skip("Test requires http3")

        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
            background_watch_delay=1.0,
            keepalive_idle_window=1.0,
            disabled_svn=disabled_svn,
        ) as p:
            assert p._background_monitoring is not None
            assert p._background_monitoring.is_alive()

            resp = p.urlopen("GET", f"{self.https_url}/get")

            assert resp.status == 200

            assert p._background_monitoring is not None
            assert p._background_monitoring.is_alive()
            assert p.num_pings == 0

            for _ in range(4):
                sleep(1.2)

                assert p._background_monitoring is not None
                assert p._background_monitoring.is_alive()

            # brief explanation on why we expect >= 1
            # and not == 4
            # multiplexed connection can receive unsolicited data
            # from remote peer. so, in that case we expect reasonably
            # that at least one ping frame will be sent in the given test.
            assert p.num_pings >= 1

        assert p._background_monitoring is None
