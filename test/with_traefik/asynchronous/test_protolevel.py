from __future__ import annotations

import socket

import pytest

from urllib3 import AsyncHTTPSConnectionPool, AsyncResolverDescription, HTTPHeaderDict
from urllib3.exceptions import InsecureRequestWarning, ProtocolError
from urllib3.util import parse_url
from urllib3.util.request import SKIP_HEADER

from .. import TraefikTestCase


@pytest.mark.asyncio
class TestProtocolLevel(TraefikTestCase):
    async def test_forbid_request_without_authority(self) -> None:
        async with AsyncHTTPSConnectionPool(
            self.host, self.https_port, ca_certs=self.ca_authority
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
            self.host, self.https_port, ca_certs=self.ca_authority
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

        resolver = AsyncResolverDescription.from_url("system://").new()

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
                assert resp.version == 30
