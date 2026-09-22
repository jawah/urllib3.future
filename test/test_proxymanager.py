from __future__ import annotations

import typing
from unittest.mock import Mock, patch

import pytest

from urllib3.backend import ResponsePromise
from urllib3.exceptions import MaxRetryError, NewConnectionError, ProxyError
from urllib3.poolmanager import ProxyManager
from urllib3.response import HTTPResponse
from urllib3.util.retry import Retry
from urllib3.util.url import parse_url

from .port_helpers import find_unused_port

if typing.TYPE_CHECKING:
    from typing_extensions import Literal


class TestProxyManager:
    @pytest.mark.parametrize("multiplexed", [False, True])
    @pytest.mark.parametrize(
        "proxy_scheme, target_scheme, forwarding, expected",
        [
            ("http", "http", False, "http://localhost/pa%23th?x=%23"),
            ("https", "http", False, "http://localhost/pa%23th?x=%23"),
            ("http", "https", False, "/pa%23th?x=%23"),
            ("https", "https", False, "/pa%23th?x=%23"),
            ("https", "https", True, "https://localhost/pa%23th?x=%23"),
        ],
    )
    def test_pool_request_target_strips_fragment(
        self,
        multiplexed: Literal[False, True],
        proxy_scheme: str,
        target_scheme: str,
        forwarding: bool,
        expected: str,
    ) -> None:
        target = f"{target_scheme}://localhost/pa%23th?x=%23#private"
        result = (
            ResponsePromise(Mock(), 1, []) if multiplexed else HTTPResponse(status=200)
        )
        with ProxyManager(
            f"{proxy_scheme}://proxy:8080", use_forwarding_for_https=forwarding
        ) as manager:
            pool = manager.connection_from_url(target)
            with patch.object(pool, "urlopen", return_value=result) as request:
                assert manager.urlopen("GET", target, multiplexed=multiplexed) is result
            assert request.call_args[0][1] == expected
            if isinstance(result, ResponsePromise):
                assert result.get_parameter("pm_url") == target

    @pytest.mark.parametrize("proxy_scheme", ["http", "https"])
    def test_proxy_headers(self, proxy_scheme: str) -> None:
        url = "http://pypi.org/project/urllib3/"
        proxy_url = f"{proxy_scheme}://something:1234"
        with ProxyManager(proxy_url) as p:
            # Verify default headers
            default_headers = {"Accept": "*/*", "Host": "pypi.org"}
            headers = p._set_proxy_headers(url)

            assert headers == default_headers

            # Verify default headers don't overwrite provided headers
            provided_headers = {
                "Accept": "application/json",
                "custom": "header",
                "Host": "test.python.org",
            }
            headers = p._set_proxy_headers(url, provided_headers)

            assert headers == provided_headers

            # Verify proxy with nonstandard port
            provided_headers = {"Accept": "application/json"}
            expected_headers = provided_headers.copy()
            expected_headers.update({"Host": "pypi.org:8080"})
            url_with_port = "http://pypi.org:8080/project/urllib3/"
            headers = p._set_proxy_headers(url_with_port, provided_headers)

            assert headers == expected_headers

    def test_default_port(self) -> None:
        with ProxyManager("http://something") as p:
            assert p.proxy is not None
            assert p.proxy.port == 80
        with ProxyManager("https://something") as p:
            assert p.proxy is not None
            assert p.proxy.port == 443

    def test_invalid_scheme(self) -> None:
        with pytest.raises(AssertionError):
            ProxyManager("invalid://host/p")
        with pytest.raises(ValueError):
            ProxyManager("invalid://host/p")

    def test_proxy_tunnel(self) -> None:
        http_url = parse_url("http://example.com")
        https_url = parse_url("https://example.com")
        with ProxyManager("http://proxy:8080") as p:
            assert p._proxy_requires_url_absolute_form(http_url)
            assert p._proxy_requires_url_absolute_form(https_url) is False

        with ProxyManager("https://proxy:8080") as p:
            assert p._proxy_requires_url_absolute_form(http_url)
            assert p._proxy_requires_url_absolute_form(https_url) is False

        with ProxyManager("https://proxy:8080", use_forwarding_for_https=True) as p:
            assert p._proxy_requires_url_absolute_form(http_url)
            assert p._proxy_requires_url_absolute_form(https_url)

    def test_proxy_connect_retry(self) -> None:
        retry = Retry(total=None, connect=False)
        port = find_unused_port()
        with ProxyManager(f"http://localhost:{port}") as p:
            with pytest.raises(ProxyError) as ei:
                p.urlopen("HEAD", url="http://localhost/", retries=retry)
            assert isinstance(ei.value.original_error, NewConnectionError)

        retry = Retry(total=None, connect=2)
        with ProxyManager(f"http://localhost:{port}") as p:
            with pytest.raises(MaxRetryError) as ei1:
                p.urlopen("HEAD", url="http://localhost/", retries=retry)
            assert ei1.value.reason is not None
            assert isinstance(ei1.value.reason, ProxyError)
            assert isinstance(ei1.value.reason.original_error, NewConnectionError)
