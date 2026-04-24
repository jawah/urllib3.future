from __future__ import annotations

import binascii
import hashlib
import ipaddress
import os.path
import pathlib
import shutil
import ssl
import tempfile
from test import LONG_TIMEOUT, SHORT_TIMEOUT
from test.conftest import ServerConfig

import pytest
import trustme

import urllib3.exceptions
from dummyserver.server import DEFAULT_CA, HAS_IPV6, get_unreachable_address
from dummyserver.testcase import HTTPDummyProxyTestCase, IPv6HTTPDummyProxyTestCase
from urllib3 import AsyncHTTPResponse, HTTPHeaderDict
from urllib3._async.poolmanager import AsyncProxyManager
from urllib3._async.poolmanager import proxy_from_url as async_proxy_from_url
from urllib3.exceptions import (
    ConnectTimeoutError,
    InsecureRequestWarning,
    MaxRetryError,
    ProxyError,
    ProxySchemeUnknown,
    ReadTimeoutError,
    SSLError,
)
from urllib3.util.ssl_ import create_urllib3_context
from urllib3.util.timeout import Timeout

from ... import TARPIT_HOST, requires_network


def assert_is_verified(
    pm: AsyncProxyManager, *, proxy: bool, target: bool
) -> None:
    pool = list(pm.pools._container.values())[-1]  # retrieve last pool entry
    connection = (
        list(pool.pool._container.values())[-1] if pool.pool is not None else None
    )  # retrieve last connection entry
    assert connection is not None
    assert connection.proxy_is_verified is proxy
    assert connection.is_verified is target


@pytest.mark.asyncio
class TestAsyncHTTPProxyManager(HTTPDummyProxyTestCase):
    @classmethod
    def setup_class(cls) -> None:
        super().setup_class()
        cls.http_url = f"http://{cls.http_host}:{int(cls.http_port)}"
        cls.http_url_alt = f"http://{cls.http_host_alt}:{int(cls.http_port)}"
        cls.https_url = f"https://{cls.https_host}:{int(cls.https_port)}"
        cls.https_url_alt = f"https://{cls.https_host_alt}:{int(cls.https_port)}"
        cls.proxy_url = f"http://{cls.proxy_host}:{int(cls.proxy_port)}"
        cls.https_proxy_url = (
            f"https://{cls.proxy_host}:{int(cls.https_proxy_port)}"
        )

        # Generate another CA to test verification failure
        cls.certs_dir = tempfile.mkdtemp()
        bad_ca = trustme.CA()

        cls.bad_ca_path = os.path.join(cls.certs_dir, "ca_bad.pem")
        bad_ca.cert_pem.write_to_path(cls.bad_ca_path)

    @classmethod
    def teardown_class(cls) -> None:
        super().teardown_class()
        shutil.rmtree(cls.certs_dir)

    async def test_basic_proxy(self) -> None:
        async with async_proxy_from_url(
            self.proxy_url, ca_certs=DEFAULT_CA
        ) as http:
            r = await http.request("GET", f"{self.http_url}/")
            assert r.status == 200

            r = await http.request("GET", f"{self.https_url}/")
            assert r.status == 200

    async def test_https_proxy(self) -> None:
        async with async_proxy_from_url(
            self.https_proxy_url, ca_certs=DEFAULT_CA
        ) as https:
            r = await https.request("GET", f"{self.https_url}/")
            assert r.status == 200

            r = await https.request("GET", f"{self.http_url}/")
            assert r.status == 200

    async def test_is_verified_http_proxy_to_http_target(self) -> None:
        async with async_proxy_from_url(
            self.proxy_url, ca_certs=DEFAULT_CA
        ) as http:
            r = await http.request("GET", f"{self.http_url}/")
            assert r.status == 200
            assert_is_verified(http, proxy=False, target=False)

    async def test_is_verified_http_proxy_to_https_target(self) -> None:
        async with async_proxy_from_url(
            self.proxy_url, ca_certs=DEFAULT_CA
        ) as http:
            r = await http.request("GET", f"{self.https_url}/")
            assert r.status == 200
            assert_is_verified(http, proxy=False, target=True)

    async def test_is_verified_https_proxy_to_http_target(self) -> None:
        async with async_proxy_from_url(
            self.https_proxy_url, ca_certs=DEFAULT_CA
        ) as https:
            r = await https.request("GET", f"{self.http_url}/")
            assert r.status == 200
            assert_is_verified(https, proxy=True, target=False)

    async def test_is_verified_https_proxy_to_https_target(self) -> None:
        async with async_proxy_from_url(
            self.https_proxy_url, ca_certs=DEFAULT_CA
        ) as https:
            r = await https.request("GET", f"{self.https_url}/")
            assert r.status == 200
            assert_is_verified(https, proxy=True, target=True)

    async def test_https_proxy_with_proxy_ssl_context(self) -> None:
        proxy_ssl_context = create_urllib3_context()
        proxy_ssl_context.load_verify_locations(DEFAULT_CA)
        async with async_proxy_from_url(
            self.https_proxy_url,
            proxy_ssl_context=proxy_ssl_context,
            ca_certs=DEFAULT_CA,
        ) as https:
            r = await https.request("GET", f"{self.https_url}/")
            assert r.status == 200

            r = await https.request("GET", f"{self.http_url}/")
            assert r.status == 200

    async def test_https_proxy_forwarding_for_https(self) -> None:
        async with async_proxy_from_url(
            self.https_proxy_url,
            ca_certs=DEFAULT_CA,
            use_forwarding_for_https=True,
        ) as https:
            r = await https.request("GET", f"{self.http_url}/")
            assert r.status == 200

            r = await https.request("GET", f"{self.https_url}/")
            assert r.status == 200

    @pytest.mark.parametrize("proxy_scheme", ["http", "https"])
    @pytest.mark.parametrize("target_scheme", ["http", "https"])
    async def test_proxy_conn_fail_from_dns(
        self, proxy_scheme: str, target_scheme: str
    ) -> None:
        host, port = get_unreachable_address()
        async with async_proxy_from_url(
            f"{proxy_scheme}://{host}:{port}/",
            retries=1,
            timeout=LONG_TIMEOUT,
        ) as http:
            if target_scheme == "https":
                target_url = self.https_url
            else:
                target_url = self.http_url

            with pytest.raises(MaxRetryError) as e:
                await http.request("GET", f"{target_url}/")
            assert type(e.value.reason) == ProxyError
            assert (
                type(e.value.reason.original_error)
                == urllib3.exceptions.NameResolutionError
            )

    async def test_redirect(self) -> None:
        async with async_proxy_from_url(self.proxy_url) as http:
            r = await http.request(
                "GET",
                f"{self.http_url}/redirect",
                fields={"target": f"{self.http_url}/"},
                redirect=False,
            )

            assert r.status == 303

            r = await http.request(
                "GET",
                f"{self.http_url}/redirect",
                fields={"target": f"{self.http_url}/"},
            )

            assert r.status == 200
            assert await r.data == b"Dummy server!"

    async def test_cross_host_redirect(self) -> None:
        async with async_proxy_from_url(self.proxy_url) as http:
            cross_host_location = f"{self.http_url_alt}/echo?a=b"
            with pytest.raises(MaxRetryError):
                await http.request(
                    "GET",
                    f"{self.http_url}/redirect",
                    fields={"target": cross_host_location},
                    retries=0,
                )

            r = await http.request(
                "GET",
                f"{self.http_url}/redirect",
                fields={"target": f"{self.http_url_alt}/echo?a=b"},
                retries=1,
            )
            assert isinstance(r, AsyncHTTPResponse)
            assert r._pool is not None
            assert r._pool.host != self.http_host_alt

    async def test_cross_protocol_redirect(self) -> None:
        async with async_proxy_from_url(
            self.proxy_url, ca_certs=DEFAULT_CA
        ) as http:
            cross_protocol_location = f"{self.https_url}/echo?a=b"
            with pytest.raises(MaxRetryError):
                await http.request(
                    "GET",
                    f"{self.http_url}/redirect",
                    fields={"target": cross_protocol_location},
                    retries=0,
                )

            r = await http.request(
                "GET",
                f"{self.http_url}/redirect",
                fields={"target": f"{self.https_url}/echo?a=b"},
                retries=1,
            )
            assert isinstance(r, AsyncHTTPResponse)
            assert r._pool is not None
            assert r._pool.host == self.https_host

    async def test_headers(self) -> None:
        async with async_proxy_from_url(
            self.proxy_url,
            headers={"Foo": "bar"},
            proxy_headers={"Hickory": "dickory"},
            ca_certs=DEFAULT_CA,
        ) as http:
            r = await http.request_encode_url(
                "GET", f"{self.http_url}/headers"
            )
            returned_headers = await r.json()
            assert returned_headers.get("Foo") == "bar"
            assert returned_headers.get("Hickory") == "dickory"
            assert (
                returned_headers.get("Host")
                == f"{self.http_host}:{self.http_port}"
            )

            r = await http.request_encode_url(
                "GET", f"{self.http_url_alt}/headers"
            )
            returned_headers = await r.json()
            assert returned_headers.get("Foo") == "bar"
            assert returned_headers.get("Hickory") == "dickory"
            assert (
                returned_headers.get("Host")
                == f"{self.http_host_alt}:{self.http_port}"
            )

            r = await http.request_encode_url(
                "GET", f"{self.https_url}/headers"
            )
            returned_headers = await r.json()
            assert returned_headers.get("Foo") == "bar"
            assert returned_headers.get("Hickory") is None
            assert (
                returned_headers.get("Host")
                == f"{self.https_host}:{self.https_port}"
            )

            r = await http.request_encode_body(
                "POST", f"{self.http_url}/headers"
            )
            returned_headers = await r.json()
            assert returned_headers.get("Foo") == "bar"
            assert returned_headers.get("Hickory") == "dickory"
            assert (
                returned_headers.get("Host")
                == f"{self.http_host}:{self.http_port}"
            )

            r = await http.request_encode_url(
                "GET",
                f"{self.http_url}/headers",
                headers={"Baz": "quux"},
            )
            returned_headers = await r.json()
            assert returned_headers.get("Foo") is None
            assert returned_headers.get("Baz") == "quux"
            assert returned_headers.get("Hickory") == "dickory"
            assert (
                returned_headers.get("Host")
                == f"{self.http_host}:{self.http_port}"
            )

            r = await http.request_encode_url(
                "GET",
                f"{self.https_url}/headers",
                headers={"Baz": "quux"},
            )
            returned_headers = await r.json()
            assert returned_headers.get("Foo") is None
            assert returned_headers.get("Baz") == "quux"
            assert returned_headers.get("Hickory") is None
            assert (
                returned_headers.get("Host")
                == f"{self.https_host}:{self.https_port}"
            )

            r = await http.request_encode_body(
                "GET",
                f"{self.http_url}/headers",
                headers={"Baz": "quux"},
            )
            returned_headers = await r.json()
            assert returned_headers.get("Foo") is None
            assert returned_headers.get("Baz") == "quux"
            assert returned_headers.get("Hickory") == "dickory"
            assert (
                returned_headers.get("Host")
                == f"{self.http_host}:{self.http_port}"
            )

            r = await http.request_encode_body(
                "GET",
                f"{self.https_url}/headers",
                headers={"Baz": "quux"},
            )
            returned_headers = await r.json()
            assert returned_headers.get("Foo") is None
            assert returned_headers.get("Baz") == "quux"
            assert returned_headers.get("Hickory") is None
            assert (
                returned_headers.get("Host")
                == f"{self.https_host}:{self.https_port}"
            )

    async def test_https_headers(self) -> None:
        async with async_proxy_from_url(
            self.https_proxy_url,
            headers={"Foo": "bar"},
            proxy_headers={"Hickory": "dickory"},
            ca_certs=DEFAULT_CA,
        ) as http:
            r = await http.request_encode_url(
                "GET", f"{self.http_url}/headers"
            )
            returned_headers = await r.json()
            assert returned_headers.get("Foo") == "bar"
            assert returned_headers.get("Hickory") == "dickory"
            assert (
                returned_headers.get("Host")
                == f"{self.http_host}:{self.http_port}"
            )

            r = await http.request_encode_url(
                "GET", f"{self.http_url_alt}/headers"
            )
            returned_headers = await r.json()
            assert returned_headers.get("Foo") == "bar"
            assert returned_headers.get("Hickory") == "dickory"
            assert (
                returned_headers.get("Host")
                == f"{self.http_host_alt}:{self.http_port}"
            )

            r = await http.request_encode_body(
                "GET",
                f"{self.https_url}/headers",
                headers={"Baz": "quux"},
            )
            returned_headers = await r.json()
            assert returned_headers.get("Foo") is None
            assert returned_headers.get("Baz") == "quux"
            assert returned_headers.get("Hickory") is None
            assert (
                returned_headers.get("Host")
                == f"{self.https_host}:{self.https_port}"
            )

    async def test_https_headers_forwarding_for_https(self) -> None:
        async with async_proxy_from_url(
            self.https_proxy_url,
            headers={"Foo": "bar"},
            proxy_headers={"Hickory": "dickory"},
            ca_certs=DEFAULT_CA,
            use_forwarding_for_https=True,
        ) as http:
            r = await http.request_encode_url(
                "GET", f"{self.https_url}/headers"
            )
            returned_headers = await r.json()
            assert returned_headers.get("Foo") == "bar"
            assert returned_headers.get("Hickory") == "dickory"
            assert (
                returned_headers.get("Host")
                == f"{self.https_host}:{self.https_port}"
            )

    async def test_headerdict(self) -> None:
        default_headers = HTTPHeaderDict(a="b")
        proxy_headers = HTTPHeaderDict()
        proxy_headers.add("foo", "bar")

        async with async_proxy_from_url(
            self.proxy_url,
            headers=default_headers,
            proxy_headers=proxy_headers,
        ) as http:
            request_headers = HTTPHeaderDict(baz="quux")
            r = await http.request(
                "GET",
                f"{self.http_url}/headers",
                headers=request_headers,
            )
            returned_headers = await r.json()
            assert returned_headers.get("Foo") == "bar"
            assert returned_headers.get("Baz") == "quux"

    async def test_proxy_pooling(self) -> None:
        async with async_proxy_from_url(
            self.proxy_url, cert_reqs="NONE"
        ) as http:
            for x in range(2):
                await http.urlopen("GET", self.http_url)
            assert len(http.pools) == 1

            for x in range(2):
                await http.urlopen("GET", self.http_url_alt)
            assert len(http.pools) == 1

            for x in range(2):
                with pytest.warns(InsecureRequestWarning):
                    await http.urlopen("GET", self.https_url)
            assert len(http.pools) == 2

            for x in range(2):
                with pytest.warns(InsecureRequestWarning):
                    await http.urlopen("GET", self.https_url_alt)
            assert len(http.pools) == 3

    @requires_network()
    @pytest.mark.parametrize(
        ["proxy_scheme", "target_scheme", "use_forwarding_for_https"],
        [
            ("http", "http", False),
            ("https", "http", False),
            # 'use_forwarding_for_https' is only valid for HTTPS+HTTPS.
            ("https", "https", True),
        ],
    )
    async def test_forwarding_proxy_request_timeout(
        self,
        proxy_scheme: str,
        target_scheme: str,
        use_forwarding_for_https: bool,
    ) -> None:
        proxy_url = (
            self.https_proxy_url
            if proxy_scheme == "https"
            else self.proxy_url
        )
        target_url = f"{target_scheme}://{TARPIT_HOST}"

        async with async_proxy_from_url(
            proxy_url,
            ca_certs=DEFAULT_CA,
            use_forwarding_for_https=use_forwarding_for_https,
        ) as proxy:
            with pytest.raises(MaxRetryError) as e:
                timeout = Timeout(connect=LONG_TIMEOUT, read=SHORT_TIMEOUT)
                await proxy.request("GET", target_url, timeout=timeout)

            # We sent the request to the proxy but didn't get any response
            # so we're not sure if that's being caused by the proxy or the
            # target so we put the blame on the target.
            assert type(e.value.reason) == ReadTimeoutError

    @requires_network()
    @pytest.mark.parametrize(
        ["proxy_scheme", "target_scheme"],
        [("http", "https"), ("https", "https")],
    )
    async def test_tunneling_proxy_request_timeout(
        self, proxy_scheme: str, target_scheme: str
    ) -> None:
        proxy_url = (
            self.https_proxy_url
            if proxy_scheme == "https"
            else self.proxy_url
        )
        target_url = f"{target_scheme}://{TARPIT_HOST}"

        async with async_proxy_from_url(
            proxy_url,
            ca_certs=DEFAULT_CA,
        ) as proxy:
            with pytest.raises(MaxRetryError) as e:
                timeout = Timeout(connect=LONG_TIMEOUT, read=SHORT_TIMEOUT)
                await proxy.request("GET", target_url, timeout=timeout)

            assert type(e.value.reason) == ReadTimeoutError

    @requires_network()
    @pytest.mark.parametrize(
        ["proxy_scheme", "target_scheme", "use_forwarding_for_https"],
        [
            ("http", "http", False),
            ("https", "http", False),
            # 'use_forwarding_for_https' is only valid for HTTPS+HTTPS.
            ("https", "https", True),
        ],
    )
    async def test_forwarding_proxy_connect_timeout(
        self,
        proxy_scheme: str,
        target_scheme: str,
        use_forwarding_for_https: bool,
    ) -> None:
        proxy_url = f"{proxy_scheme}://{TARPIT_HOST}"
        target_url = (
            self.https_url if target_scheme == "https" else self.http_url
        )

        async with async_proxy_from_url(
            proxy_url,
            ca_certs=DEFAULT_CA,
            timeout=SHORT_TIMEOUT,
            use_forwarding_for_https=use_forwarding_for_https,
        ) as proxy:
            with pytest.raises(MaxRetryError) as e:
                await proxy.request("GET", target_url)

            assert type(e.value.reason) == ProxyError
            assert type(e.value.reason.original_error) == ConnectTimeoutError

    @requires_network()
    @pytest.mark.parametrize(
        ["proxy_scheme", "target_scheme"],
        [("http", "https"), ("https", "https")],
    )
    async def test_tunneling_proxy_connect_timeout(
        self, proxy_scheme: str, target_scheme: str
    ) -> None:
        proxy_url = f"{proxy_scheme}://{TARPIT_HOST}"
        target_url = (
            self.https_url if target_scheme == "https" else self.http_url
        )

        async with async_proxy_from_url(
            proxy_url, ca_certs=DEFAULT_CA, timeout=SHORT_TIMEOUT
        ) as proxy:
            with pytest.raises(MaxRetryError) as e:
                await proxy.request("GET", target_url)

            assert type(e.value.reason) == ProxyError
            assert type(e.value.reason.original_error) == ConnectTimeoutError

    @requires_network()
    @pytest.mark.parametrize(
        ["target_scheme", "use_forwarding_for_https"],
        [
            ("http", False),
            ("https", False),
            ("https", True),
        ],
    )
    async def test_https_proxy_tls_error(
        self, target_scheme: str, use_forwarding_for_https: str
    ) -> None:
        target_url = (
            self.https_url if target_scheme == "https" else self.http_url
        )
        proxy_ctx = ssl.create_default_context()
        async with async_proxy_from_url(
            self.https_proxy_url,
            proxy_ssl_context=proxy_ctx,
            use_forwarding_for_https=use_forwarding_for_https,
        ) as proxy:
            with pytest.raises(MaxRetryError) as e:
                await proxy.request("GET", target_url)
            assert type(e.value.reason) == ProxyError
            assert type(e.value.reason.original_error) == SSLError

    @requires_network()
    @pytest.mark.parametrize(
        ["proxy_scheme", "use_forwarding_for_https"],
        [
            ("http", False),
            ("https", False),
            ("https", True),
        ],
    )
    async def test_proxy_https_target_tls_error(
        self, proxy_scheme: str, use_forwarding_for_https: str
    ) -> None:
        if proxy_scheme == "https" and use_forwarding_for_https:
            pytest.skip(
                "Test is expected to fail due to urllib3/urllib3#2577"
            )

        try:
            import rtls as alt_ssl
        except ImportError:
            alt_ssl = None  # type: ignore[assignment]

        proxy_url = (
            self.https_proxy_url
            if proxy_scheme == "https"
            else self.proxy_url
        )
        if alt_ssl is None:
            proxy_ctx = ssl.create_default_context()
        else:
            proxy_ctx = alt_ssl.create_default_context()
        proxy_ctx.load_verify_locations(DEFAULT_CA)
        if alt_ssl is None:
            ctx = ssl.create_default_context()
        else:
            ctx = alt_ssl.create_default_context()

        async with async_proxy_from_url(
            proxy_url,
            proxy_ssl_context=proxy_ctx,
            ssl_context=ctx,
            use_forwarding_for_https=use_forwarding_for_https,
        ) as proxy:
            with pytest.raises(MaxRetryError) as e:
                await proxy.request("GET", self.https_url)
            assert type(e.value.reason) == SSLError

    async def test_scheme_host_case_insensitive(self) -> None:
        """Assert that upper-case schemes and hosts are normalized."""
        async with async_proxy_from_url(
            self.proxy_url.upper(), ca_certs=DEFAULT_CA
        ) as http:
            r = await http.request("GET", f"{self.http_url.upper()}/")
            assert r.status == 200

            r = await http.request("GET", f"{self.https_url.upper()}/")
            assert r.status == 200

    @pytest.mark.parametrize(
        "url, error_msg",
        [
            (
                "127.0.0.1",
                "Proxy URL had no scheme, should start with"
                " http:// or https://",
            ),
            (
                "localhost:8080",
                "Proxy URL had no scheme, should start with"
                " http:// or https://",
            ),
            (
                "ftp://google.com",
                "Proxy URL had unsupported scheme ftp, should use"
                " http:// or https://",
            ),
        ],
    )
    async def test_invalid_schema(
        self, url: str, error_msg: str
    ) -> None:
        with pytest.raises(ProxySchemeUnknown, match=error_msg):
            async_proxy_from_url(url)


@pytest.mark.asyncio
@pytest.mark.skipif(not HAS_IPV6, reason="Only runs on IPv6 systems")
class TestAsyncIPv6HTTPProxyManager(IPv6HTTPDummyProxyTestCase):
    @classmethod
    def setup_class(cls) -> None:
        HTTPDummyProxyTestCase.setup_class()
        cls.http_url = f"http://{cls.http_host}:{int(cls.http_port)}"
        cls.http_url_alt = f"http://{cls.http_host_alt}:{int(cls.http_port)}"
        cls.https_url = f"https://{cls.https_host}:{int(cls.https_port)}"
        cls.https_url_alt = (
            f"https://{cls.https_host_alt}:{int(cls.https_port)}"
        )
        cls.proxy_url = (
            f"http://[{cls.proxy_host}]:{int(cls.proxy_port)}"
        )

    async def test_basic_ipv6_proxy(self) -> None:
        async with async_proxy_from_url(
            self.proxy_url, ca_certs=DEFAULT_CA
        ) as http:
            r = await http.request("GET", f"{self.http_url}/")
            assert r.status == 200

            r = await http.request("GET", f"{self.https_url}/")
            assert r.status == 200


@pytest.mark.asyncio
class TestAsyncHTTPSProxyVerification:
    @staticmethod
    def _get_proxy_fingerprint_md5(ca_path: str) -> str:
        proxy_pem_path = pathlib.Path(ca_path).parent / "proxy.pem"
        proxy_der = ssl.PEM_cert_to_DER_cert(proxy_pem_path.read_text())
        proxy_hashed = hashlib.md5(proxy_der).digest()
        fingerprint = binascii.hexlify(proxy_hashed).decode("ascii")
        return fingerprint

    @staticmethod
    def _get_certificate_formatted_proxy_host(host: str) -> str:
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return host

        if addr.version != 6:
            return host

        # Transform ipv6 like '::1' to 0:0:0:0:0:0:0:1
        # via '0000:0000:0000:0000:0000:0000:0000:0001'
        return addr.exploded.replace("0000", "0").replace("000", "")

    async def test_https_proxy_assert_fingerprint_md5(
        self,
        no_san_proxy_with_server: tuple[ServerConfig, ServerConfig],
    ) -> None:
        proxy, server = no_san_proxy_with_server
        proxy_url = f"https://{proxy.host}:{proxy.port}"
        destination_url = f"https://{server.host}:{server.port}"

        proxy_fingerprint = self._get_proxy_fingerprint_md5(
            proxy.ca_certs  # type: ignore[arg-type]
        )
        async with async_proxy_from_url(
            proxy_url,
            ca_certs=proxy.ca_certs,
            proxy_assert_fingerprint=proxy_fingerprint,
        ) as https:
            await https.request("GET", destination_url)

    async def test_https_proxy_assert_fingerprint_md5_non_matching(
        self,
        no_san_proxy_with_server: tuple[ServerConfig, ServerConfig],
    ) -> None:
        proxy, server = no_san_proxy_with_server
        proxy_url = f"https://{proxy.host}:{proxy.port}"
        destination_url = f"https://{server.host}:{server.port}"

        proxy_fingerprint = self._get_proxy_fingerprint_md5(
            proxy.ca_certs  # type: ignore[arg-type]
        )
        new_char = "b" if proxy_fingerprint[5] == "a" else "a"
        proxy_fingerprint = (
            proxy_fingerprint[:5] + new_char + proxy_fingerprint[6:]
        )

        async with async_proxy_from_url(
            proxy_url,
            ca_certs=proxy.ca_certs,
            proxy_assert_fingerprint=proxy_fingerprint,
        ) as https:
            with pytest.raises(MaxRetryError) as e:
                await https.request("GET", destination_url)

            assert "Fingerprints did not match" in str(e)

    async def test_https_proxy_assert_hostname(
        self,
        san_proxy_with_server: tuple[ServerConfig, ServerConfig],
    ) -> None:
        proxy, server = san_proxy_with_server
        destination_url = f"https://{server.host}:{server.port}"

        async with async_proxy_from_url(
            proxy.base_url,
            ca_certs=proxy.ca_certs,
            proxy_assert_hostname=proxy.host,
        ) as https:
            await https.request("GET", destination_url)

    async def test_https_proxy_assert_hostname_non_matching(
        self,
        san_proxy_with_server: tuple[ServerConfig, ServerConfig],
    ) -> None:
        proxy, server = san_proxy_with_server
        destination_url = f"https://{server.host}:{server.port}"

        proxy_hostname = "example.com"
        async with async_proxy_from_url(
            proxy.base_url,
            ca_certs=proxy.ca_certs,
            proxy_assert_hostname=proxy_hostname,
        ) as https:
            with pytest.raises(MaxRetryError) as e:
                await https.request("GET", destination_url)

            proxy_host = self._get_certificate_formatted_proxy_host(
                proxy.host
            )
            msg = (
                f"hostname \\'{proxy_hostname}\\'"
                f" doesn\\'t match \\'{proxy_host}"
            )
            assert msg in str(e)

    async def test_https_proxy_hostname_verification(
        self, no_localhost_san_server: ServerConfig
    ) -> None:
        bad_server = no_localhost_san_server
        bad_proxy_url = (
            f"https://{bad_server.host}:{bad_server.port}"
        )

        # An exception will be raised before we contact the destination
        # domain.
        test_url = "testing.com"
        async with async_proxy_from_url(
            bad_proxy_url, ca_certs=bad_server.ca_certs
        ) as https:
            with pytest.raises(MaxRetryError) as e:
                await https.request("GET", "http://%s/" % test_url)
            assert isinstance(e.value.reason, ProxyError)

            ssl_error = e.value.reason.original_error
            assert isinstance(ssl_error, SSLError)
            assert (
                "hostname 'localhost' doesn't match" in str(ssl_error)
                or "Hostname mismatch" in str(ssl_error)
                or "invalid peer certificate: certificate not valid for name"
                in str(ssl_error)
            )

            with pytest.raises(MaxRetryError) as e:
                await https.request("GET", "https://%s/" % test_url)
            assert isinstance(e.value.reason, ProxyError)

            ssl_error = e.value.reason.original_error
            assert isinstance(ssl_error, SSLError)
            assert (
                "hostname 'localhost' doesn't match" in str(ssl_error)
                or "Hostname mismatch" in str(ssl_error)
                or "invalid peer certificate: certificate not valid for name"
                in str(ssl_error)
            )

    async def test_https_proxy_ipv4_san(
        self,
        ipv4_san_proxy_with_server: tuple[ServerConfig, ServerConfig],
    ) -> None:
        proxy, server = ipv4_san_proxy_with_server
        proxy_url = f"https://{proxy.host}:{proxy.port}"
        destination_url = f"https://{server.host}:{server.port}"
        async with async_proxy_from_url(
            proxy_url, ca_certs=proxy.ca_certs
        ) as https:
            r = await https.request("GET", destination_url)
            assert r.status == 200

    @pytest.mark.skipif(
        HAS_IPV6 is False, reason="Only runs on IPv6 systems"
    )
    async def test_https_proxy_ipv6_san(
        self,
        ipv6_san_proxy_with_server: tuple[ServerConfig, ServerConfig],
    ) -> None:
        proxy, server = ipv6_san_proxy_with_server
        proxy_url = f"https://[{proxy.host}]:{proxy.port}"
        destination_url = f"https://{server.host}:{server.port}"
        async with async_proxy_from_url(
            proxy_url, ca_certs=proxy.ca_certs
        ) as https:
            r = await https.request("GET", destination_url)
            assert r.status == 200

    @pytest.mark.parametrize("target_scheme", ["http", "https"])
    async def test_https_proxy_no_san(
        self,
        no_san_proxy_with_server: tuple[ServerConfig, ServerConfig],
        target_scheme: str,
    ) -> None:
        proxy, server = no_san_proxy_with_server
        proxy_url = f"https://{proxy.host}:{proxy.port}"
        destination_url = (
            f"{target_scheme}://{server.host}:{server.port}"
        )

        async with async_proxy_from_url(
            proxy_url, ca_certs=proxy.ca_certs
        ) as https:
            with pytest.raises(MaxRetryError) as e:
                await https.request("GET", destination_url)
            assert isinstance(e.value.reason, ProxyError)

            ssl_error = e.value.reason.original_error
            assert isinstance(ssl_error, SSLError)
            assert (
                "no appropriate subjectAltName fields were found"
                in str(ssl_error)
                or "Hostname mismatch, certificate is not valid"
                " for 'localhost'" in str(ssl_error)
                or "invalid peer certificate: certificate not valid"
                " for name" in str(ssl_error)
            )

    async def test_https_proxy_no_san_hostname_checks_common_name(
        self,
        no_san_proxy_with_server: tuple[ServerConfig, ServerConfig],
    ) -> None:
        proxy, server = no_san_proxy_with_server
        proxy_url = f"https://{proxy.host}:{proxy.port}"
        destination_url = f"https://{server.host}:{server.port}"

        proxy_ctx = urllib3.util.ssl_.create_urllib3_context()
        try:
            proxy_ctx.hostname_checks_common_name = True
        # PyPy doesn't like us setting 'hostname_checks_common_name'
        # but also has it enabled by default so we need to handle that.
        except AttributeError:
            pass
        if (
            getattr(proxy_ctx, "hostname_checks_common_name", False)
            is not True
        ):
            pytest.skip(
                "Test requires"
                " 'SSLContext.hostname_checks_common_name=True'"
            )

        async with async_proxy_from_url(
            proxy_url,
            ca_certs=proxy.ca_certs,
            proxy_ssl_context=proxy_ctx,
        ) as https:
            await https.request("GET", destination_url)
