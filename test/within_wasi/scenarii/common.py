from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urllib3.contrib.resolver import BaseResolver
    from urllib3.contrib.resolver._async import AsyncBaseResolver


HTTP_URL = os.environ.get("WASI_HTTP_URL", "http://httpbin.local:8888")
HTTPS_URL = os.environ.get("WASI_HTTPS_URL", "https://httpbin.local:4443")
TLS12_URL = os.environ.get("WASI_TLS12_URL", "https://localhost:8445")
MTLS_URL = os.environ.get("WASI_MTLS_URL", "https://localhost:8444")
HTTP_PROXY_URL = os.environ.get("WASI_HTTP_PROXY_URL", "http://127.0.0.1:18080")
HTTPS_PROXY_URL = os.environ.get("WASI_HTTPS_PROXY_URL", "https://localhost:18443")
SOCKS_PROXY_URL = os.environ.get("WASI_SOCKS_PROXY_URL", "socks5h://127.0.0.1:19080")

ROOT_CA = "fixtures/root-ca.pem"
COMBINED_CA = "fixtures/combined-ca.pem"
CLIENT_CERT = "fixtures/client.pem"
CLIENT_KEY = "fixtures/client.key"


def sync_resolver() -> BaseResolver:
    from urllib3.contrib.resolver import ResolverDescription

    return ResolverDescription.from_url(
        "in-memory://default/?hosts=httpbin.local:127.0.0.1,"
        "alt.httpbin.local:127.0.0.1,localhost:127.0.0.1"
    ).new()


def async_resolver() -> AsyncBaseResolver:
    from urllib3.contrib.resolver._async import AsyncResolverDescription

    return AsyncResolverDescription.from_url(
        "in-memory://default/?hosts=httpbin.local:127.0.0.1,"
        "alt.httpbin.local:127.0.0.1,localhost:127.0.0.1"
    ).new()
