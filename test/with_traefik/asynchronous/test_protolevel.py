from __future__ import annotations

import socket

import pytest

from urllib3 import AsyncHTTPSConnectionPool, HTTPHeaderDict, HttpVersion
from urllib3.backend.hface import _HAS_HTTP3_SUPPORT
from urllib3.exceptions import InsecureRequestWarning, ProtocolError
from urllib3.util import parse_url
from urllib3.util.request import SKIP_HEADER

from .. import TraefikTestCase


@pytest.mark.asyncio
class TestProtocolLevel(TraefikTestCase):
    async def test_forbid_request_without_authority(self) -> None:
        async with AsyncHTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver,
        ) as p:
            with pytest.raises(
                ProtocolError,
                match="do not support emitting HTTP requests without the `Host` header",
            ):
                await p.request(
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
    async def test_headers(self, headers: list[tuple[str, str]]) -> None:
        dict_headers = dict(headers)

        async with AsyncHTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver,
        ) as p:
            resp = await p.request(
                "GET",
                f"{self.https_url}/headers",
                headers=dict_headers,
                retries=False,
            )

            assert resp.status == 200

            temoin = HTTPHeaderDict(dict_headers)
            payload = await resp.json()

            seen = []

            for key, value in payload["headers"].items():
                if key in temoin:
                    seen.append(key)
                    assert temoin.get(key) in value

            assert len(seen) == len(dict_headers.keys())

    async def test_override_authority_via_host_header(self) -> None:
        assert self.https_url is not None

        parsed_url = parse_url(self.https_url)
        assert parsed_url.host is not None

        resolver = self.test_async_resolver.new()

        records = await resolver.getaddrinfo(
            parsed_url.host,
            parsed_url.port,
            socket.AF_INET,
            socket.SOCK_STREAM,
        )

        target_ip = records[0][-1][0]

        with pytest.warns(InsecureRequestWarning):
            async with AsyncHTTPSConnectionPool(
                target_ip, self.https_port, ca_certs=self.ca_authority, cert_reqs=0
            ) as p:
                resp = await p.request(
                    "GET",
                    f"{self.https_url.replace(parsed_url.host, target_ip)}/get",
                    headers={"host": parsed_url.host},
                    retries=False,
                )

                assert resp.status == 200
                assert resp.version == 20

                resp = await p.request(
                    "GET",
                    f"{self.https_url.replace(parsed_url.host, target_ip)}/get",
                    headers={"host": parsed_url.host},
                    retries=False,
                )

                assert resp.status == 200
                assert resp.version == 30 if _HAS_HTTP3_SUPPORT() else 20

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
    async def test_http_trailers(
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

        async with AsyncHTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver,
            disabled_svn=disabled_svn,
        ) as p:
            resp = await p.request_encode_url(
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
