from __future__ import annotations

from test import LONG_TIMEOUT

import pytest

from dummyserver.server import HAS_IPV6
from dummyserver.testcase import HTTPDummyServerTestCase, IPv6HTTPDummyServerTestCase
from urllib3 import AsyncHTTPResponse, HTTPHeaderDict
from urllib3._async.poolmanager import AsyncPoolManager
from urllib3.connectionpool import port_by_scheme
from urllib3.exceptions import MaxRetryError, URLSchemeUnknown
from urllib3.util.retry import Retry


@pytest.mark.asyncio
class TestAsyncPoolManager(HTTPDummyServerTestCase):
    @classmethod
    def setup_class(cls) -> None:
        super().setup_class()
        cls.base_url = f"http://{cls.host}:{cls.port}"
        cls.base_url_alt = f"http://{cls.host_alt}:{cls.port}"

    async def test_redirect(self) -> None:
        async with AsyncPoolManager() as http:
            r = await http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": f"{self.base_url}/"},
                redirect=False,
            )

            assert r.status == 303

            r = await http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": f"{self.base_url}/"},
            )

            assert r.status == 200
            assert await r.data == b"Dummy server!"

    async def test_redirect_with_alt_top_level(self) -> None:
        from urllib3_future import AsyncPoolManager as APM  # type: ignore

        async with APM() as http:
            r = await http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": f"{self.base_url}/"},
                redirect=False,
            )

            assert r.status == 303

            r = await http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": f"{self.base_url}/"},
            )

            assert r.status == 200
            assert await r.data == b"Dummy server!"

    async def test_redirect_twice(self) -> None:
        async with AsyncPoolManager() as http:
            r = await http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": f"{self.base_url}/redirect"},
                redirect=False,
            )

            assert r.status == 303

            r = await http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": f"{self.base_url}/redirect?target={self.base_url}/"},
            )

            assert r.status == 200
            assert await r.data == b"Dummy server!"

    async def test_redirect_to_relative_url(self) -> None:
        async with AsyncPoolManager() as http:
            r = await http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": "/redirect"},
                redirect=False,
            )

            assert r.status == 303

            r = await http.request(
                "GET", f"{self.base_url}/redirect", fields={"target": "/redirect"}
            )

            assert r.status == 200
            assert await r.data == b"Dummy server!"

    async def test_cross_host_redirect(self) -> None:
        async with AsyncPoolManager() as http:
            cross_host_location = f"{self.base_url_alt}/echo?a=b"
            with pytest.raises(MaxRetryError):
                await http.request(
                    "GET",
                    f"{self.base_url}/redirect",
                    fields={"target": cross_host_location},
                    timeout=LONG_TIMEOUT,
                    retries=0,
                )

            r = await http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": f"{self.base_url_alt}/echo?a=b"},
                timeout=LONG_TIMEOUT,
                retries=1,
            )

            assert isinstance(r, AsyncHTTPResponse)
            assert r._pool is not None
            assert r._pool.host == self.host_alt

    async def test_too_many_redirects(self) -> None:
        async with AsyncPoolManager() as http:
            with pytest.raises(MaxRetryError):
                await http.request(
                    "GET",
                    f"{self.base_url}/redirect",
                    fields={
                        "target": f"{self.base_url}/redirect?target={self.base_url}/"
                    },
                    retries=1,
                    preload_content=False,
                )

            with pytest.raises(MaxRetryError):
                await http.request(
                    "GET",
                    f"{self.base_url}/redirect",
                    fields={
                        "target": f"{self.base_url}/redirect?target={self.base_url}/"
                    },
                    retries=Retry(total=None, redirect=1),
                    preload_content=False,
                )

            # Even with preload_content=False and raise on redirects, we reused the same
            # connection
            assert len(http.pools) == 1
            pool = await http.connection_from_host(self.host, self.port)
            assert pool.num_connections == 1

        # Check when retries are configured for the pool manager.
        async with AsyncPoolManager(retries=1) as http:
            with pytest.raises(MaxRetryError):
                await http.request(
                    "GET",
                    f"{self.base_url}/redirect",
                    fields={"target": f"/redirect?target={self.base_url}/"},
                )

            # Here we allow more retries for the request.
            response = await http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": f"/redirect?target={self.base_url}/"},
                retries=2,
            )
            assert response.status == 200

    async def test_redirect_cross_host_remove_headers(self) -> None:
        async with AsyncPoolManager() as http:
            r = await http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": f"{self.base_url_alt}/headers"},
                headers={
                    "Authorization": "foo",
                    "Proxy-Authorization": "bar",
                    "Cookie": "foo=bar",
                },
            )

            assert r.status == 200

            data = await r.json()

            assert "Authorization" not in data
            assert "Proxy-Authorization" not in data
            assert "Cookie" not in data

            r = await http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": f"{self.base_url_alt}/headers"},
                headers={
                    "authorization": "foo",
                    "proxy-authorization": "baz",
                    "cookie": "foo=bar",
                },
            )

            assert r.status == 200

            data = await r.json()

            assert "authorization" not in data
            assert "Authorization" not in data
            assert "proxy-authorization" not in data
            assert "Proxy-Authorization" not in data
            assert "cookie" not in data
            assert "Cookie" not in data

    async def test_redirect_cross_host_no_remove_headers(self) -> None:
        async with AsyncPoolManager() as http:
            r = await http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": f"{self.base_url_alt}/headers"},
                headers={
                    "Authorization": "foo",
                    "Proxy-Authorization": "bar",
                    "Cookie": "foo=bar",
                },
                retries=Retry(remove_headers_on_redirect=[]),
            )

            assert r.status == 200

            data = await r.json()

            assert data["Authorization"] == "foo"
            assert data["Proxy-Authorization"] == "bar"
            assert data["Cookie"] == "foo=bar"

    async def test_redirect_cross_host_set_removed_headers(self) -> None:
        async with AsyncPoolManager() as http:
            r = await http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": f"{self.base_url_alt}/headers"},
                headers={
                    "X-API-Secret": "foo",
                    "Authorization": "bar",
                    "Proxy-Authorization": "baz",
                    "Cookie": "foo=bar",
                },
                retries=Retry(remove_headers_on_redirect=["X-API-Secret"]),
            )

            assert r.status == 200

            data = await r.json()

            assert "X-API-Secret" not in data
            assert data["Authorization"] == "bar"
            assert data["Proxy-Authorization"] == "baz"
            assert data["Cookie"] == "foo=bar"

            headers = {
                "x-api-secret": "foo",
                "authorization": "bar",
                "proxy-authorization": "baz",
                "cookie": "foo=bar",
            }
            r = await http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": f"{self.base_url_alt}/headers"},
                headers=headers,
                retries=Retry(remove_headers_on_redirect=["X-API-Secret"]),
            )

            assert r.status == 200

            data = await r.json()

            assert "x-api-secret" not in data
            assert "X-API-Secret" not in data
            assert data["Authorization"] == "bar"
            assert data["Proxy-Authorization"] == "baz"
            assert data["Cookie"] == "foo=bar"

            # Ensure the header argument itself is not modified in-place.
            assert headers == {
                "x-api-secret": "foo",
                "authorization": "bar",
                "proxy-authorization": "baz",
                "cookie": "foo=bar",
            }

    async def test_redirect_without_preload_releases_connection(self) -> None:
        async with AsyncPoolManager(block=True, maxsize=2) as http:
            r = await http.request(
                "GET", f"{self.base_url}/redirect", preload_content=False
            )
            assert isinstance(r, AsyncHTTPResponse)
            assert r._pool is not None
            assert r._pool.num_requests == 2
            assert r._pool.num_connections == 1
            assert len(http.pools) == 1
            await r.data  # consume content, avoid resource warning

    async def test_303_redirect_makes_request_lose_body(self) -> None:
        async with AsyncPoolManager() as http:
            response = await http.request(
                "POST",
                f"{self.base_url}/redirect",
                fields={
                    "target": f"{self.base_url}/headers_and_params",
                    "status": "303 See Other",
                },
            )
            data = await response.json()
            assert data["params"] == {}
            assert "Content-Type" not in HTTPHeaderDict(data["headers"])
            await response.data  # consume content, avoid resource warning

    async def test_unknown_scheme(self) -> None:
        async with AsyncPoolManager() as http:
            unknown_scheme = "unknown"
            unknown_scheme_url = f"{unknown_scheme}://host"
            with pytest.raises(URLSchemeUnknown) as e:
                r = await http.request("GET", unknown_scheme_url)
            assert e.value.scheme == unknown_scheme
            r = await http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": unknown_scheme_url},
                redirect=False,
            )
            assert r.status == 303
            assert r.headers.get("Location") == unknown_scheme_url
            with pytest.raises(URLSchemeUnknown) as e:
                r = await http.request(
                    "GET",
                    f"{self.base_url}/redirect",
                    fields={"target": unknown_scheme_url},
                )
            assert e.value.scheme == unknown_scheme

    async def test_raise_on_redirect(self) -> None:
        async with AsyncPoolManager() as http:
            r = await http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": f"{self.base_url}/redirect?target={self.base_url}/"},
                retries=Retry(total=None, redirect=1, raise_on_redirect=False),
            )

            assert r.status == 303

    async def test_raise_on_status(self) -> None:
        async with AsyncPoolManager() as http:
            with pytest.raises(MaxRetryError):
                # the default is to raise
                r = await http.request(
                    "GET",
                    f"{self.base_url}/status",
                    fields={"status": "500 Internal Server Error"},
                    retries=Retry(total=1, status_forcelist=range(500, 600)),
                )

            with pytest.raises(MaxRetryError):
                # raise explicitly
                r = await http.request(
                    "GET",
                    f"{self.base_url}/status",
                    fields={"status": "500 Internal Server Error"},
                    retries=Retry(
                        total=1, status_forcelist=range(500, 600), raise_on_status=True
                    ),
                )

            # don't raise
            r = await http.request(
                "GET",
                f"{self.base_url}/status",
                fields={"status": "500 Internal Server Error"},
                retries=Retry(
                    total=1, status_forcelist=range(500, 600), raise_on_status=False
                ),
            )

            assert r.status == 500

    async def test_missing_port(self) -> None:
        # Can a URL that lacks an explicit port like ':80' succeed, or
        # will all such URLs fail with an error?

        async with AsyncPoolManager() as http:
            # By globally adjusting `port_by_scheme` we pretend for a moment
            # that HTTP's default port is not 80, but is the port at which
            # our test server happens to be listening.
            port_by_scheme["http"] = self.port
            try:
                r = await http.request("GET", f"http://{self.host}/", retries=0)
            finally:
                port_by_scheme["http"] = 80

            assert r.status == 200
            assert await r.data == b"Dummy server!"

    async def test_headers(self) -> None:
        async with AsyncPoolManager(headers={"Foo": "bar"}) as http:
            r = await http.request("GET", f"{self.base_url}/headers")
            returned_headers = await r.json()
            assert returned_headers.get("Foo") == "bar"

            r = await http.request("POST", f"{self.base_url}/headers")
            returned_headers = await r.json()
            assert returned_headers.get("Foo") == "bar"

            r = await http.request_encode_url("GET", f"{self.base_url}/headers")
            returned_headers = await r.json()
            assert returned_headers.get("Foo") == "bar"

            r = await http.request_encode_body("POST", f"{self.base_url}/headers")
            returned_headers = await r.json()
            assert returned_headers.get("Foo") == "bar"

            r = await http.request_encode_url(
                "GET", f"{self.base_url}/headers", headers={"Baz": "quux"}
            )
            returned_headers = await r.json()
            assert returned_headers.get("Foo") is None
            assert returned_headers.get("Baz") == "quux"

            r = await http.request_encode_body(
                "GET", f"{self.base_url}/headers", headers={"Baz": "quux"}
            )
            returned_headers = await r.json()
            assert returned_headers.get("Foo") is None
            assert returned_headers.get("Baz") == "quux"

    async def test_headers_http_header_dict(self) -> None:
        # Test uses a list of headers to assert the order
        # that headers are sent in the request too.

        headers = HTTPHeaderDict()
        headers.add("Foo", "bar")
        headers.add("Multi", "1")
        headers.add("Baz", "quux")
        headers.add("Multi", "2")

        async with AsyncPoolManager(headers=headers) as http:
            r = await http.request("GET", f"{self.base_url}/multi_headers")
            returned_headers = (await r.json())["headers"]
            assert returned_headers[-4:] == [
                ["Foo", "bar"],
                ["Multi", "1"],
                ["Multi", "2"],
                ["Baz", "quux"],
            ]

            r = await http.request(
                "GET",
                f"{self.base_url}/multi_headers",
                headers={
                    **headers,
                    "Extra": "extra",
                    "Foo": "new",
                },
            )
            returned_headers = (await r.json())["headers"]
            assert returned_headers[-4:] == [
                ["Foo", "new"],
                ["Multi", "1, 2"],
                ["Baz", "quux"],
                ["Extra", "extra"],
            ]

    async def test_headers_http_multi_header_multipart(self) -> None:
        headers = HTTPHeaderDict()
        headers.add("Multi", "1")
        headers.add("Multi", "2")
        old_headers = headers.copy()

        async with AsyncPoolManager(headers=headers) as http:
            r = await http.request(
                "POST",
                f"{self.base_url}/multi_headers",
                fields={"k": "v"},
                multipart_boundary="b",
                encode_multipart=True,
            )
            returned_headers = (await r.json())["headers"]
            assert returned_headers[4:] == [
                ["Multi", "1"],
                ["Multi", "2"],
                ["Content-Type", "multipart/form-data; boundary=b"],
            ]
            # Assert that the previous headers weren't modified.
            assert headers == old_headers

            # Set a default value for the Content-Type
            headers["Content-Type"] = "multipart/form-data; boundary=b; field=value"
            r = await http.request(
                "POST",
                f"{self.base_url}/multi_headers",
                fields={"k": "v"},
                multipart_boundary="b",
                encode_multipart=True,
            )
            returned_headers = (await r.json())["headers"]
            assert returned_headers[4:] == [
                ["Multi", "1"],
                ["Multi", "2"],
                # Uses the set value, not the one that would be generated.
                ["Content-Type", "multipart/form-data; boundary=b; field=value"],
            ]

    async def test_body(self) -> None:
        async with AsyncPoolManager() as http:
            r = await http.request("POST", f"{self.base_url}/echo", body=b"test")
            assert await r.data == b"test"

    async def test_http_with_ssl_keywords(self) -> None:
        async with AsyncPoolManager(ca_certs="REQUIRED") as http:
            r = await http.request("GET", f"http://{self.host}:{self.port}/")
            assert r.status == 200

    async def test_http_with_server_hostname(self) -> None:
        async with AsyncPoolManager(server_hostname="example.com") as http:
            r = await http.request("GET", f"http://{self.host}:{self.port}/")
            assert r.status == 200

    async def test_http_with_ca_cert_dir(self) -> None:
        async with AsyncPoolManager(
            ca_certs="REQUIRED", ca_cert_dir="/nosuchdir"
        ) as http:
            r = await http.request("GET", f"http://{self.host}:{self.port}/")
            assert r.status == 200

    @pytest.mark.parametrize(
        ["target", "expected_target"],
        [
            ("/echo_uri?q=1#fragment", b"/echo_uri?q=1"),
            ("/echo_uri?#", b"/echo_uri?"),
            ("/echo_uri#?", b"/echo_uri"),
            ("/echo_uri#?#", b"/echo_uri"),
            ("/echo_uri??#", b"/echo_uri??"),
            ("/echo_uri?%3f#", b"/echo_uri?%3F"),
            ("/echo_uri?%3F#", b"/echo_uri?%3F"),
            ("/echo_uri?[]", b"/echo_uri?%5B%5D"),
        ],
    )
    async def test_encode_http_target(
        self, target: str, expected_target: bytes
    ) -> None:
        async with AsyncPoolManager() as http:
            url = f"http://{self.host}:{self.port}{target}"
            r = await http.request("GET", url)
            assert await r.data == expected_target


@pytest.mark.skipif(not HAS_IPV6, reason="IPv6 is not supported on this system")
@pytest.mark.asyncio
class TestIPv6AsyncPoolManager(IPv6HTTPDummyServerTestCase):
    @classmethod
    def setup_class(cls) -> None:
        super().setup_class()
        cls.base_url = f"http://[{cls.host}]:{cls.port}"

    async def test_ipv6(self) -> None:
        async with AsyncPoolManager() as http:
            await http.request("GET", self.base_url)
