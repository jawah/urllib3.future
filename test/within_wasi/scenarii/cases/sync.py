from __future__ import annotations

import base64
import io
import unittest
import warnings

from urllib3 import ConnectionInfo, HttpVersion, PoolManager, proxy_from_url
from urllib3.contrib.socks import SOCKSProxyManager
from urllib3.contrib.anytls import ssl
from urllib3.contrib.webextensions import (
    ServerSideEventExtensionFromHTTP,
    WebSocketExtensionFromHTTP,
)
from urllib3.exceptions import InsecureRequestWarning

from ..common import (
    CLIENT_CERT,
    CLIENT_KEY,
    COMBINED_CA,
    HTTP_PROXY_URL,
    HTTP_URL,
    HTTPS_PROXY_URL,
    HTTPS_URL,
    MTLS_URL,
    ROOT_CA,
    SOCKS_PROXY_URL,
    TLS12_URL,
    sync_resolver,
)


class SyncWasiTests(unittest.TestCase):
    __test__ = False

    def test_methods_bodies(self) -> None:
        for base_url, ca_certs in ((HTTP_URL, None), (HTTPS_URL, ROOT_CA)):
            bodies: dict[str, bytes | io.BytesIO] = {
                "POST": b"post bytes",
                "PUT": io.BytesIO(b"put BytesIO"),
                "PATCH": io.BytesIO(b"patch BytesIO"),
            }
            with PoolManager(ca_certs=ca_certs, resolver=sync_resolver()) as pool:
                for method in ("GET", "DELETE"):
                    with self.subTest(url=base_url, method=method):
                        response = pool.urlopen(method, f"{base_url}/{method.lower()}")
                        self.assertEqual(response.status, 200)

                for method, body in bodies.items():
                    with self.subTest(
                        url=base_url, method=method, body=type(body).__name__
                    ):
                        response = pool.urlopen(
                            method, f"{base_url}/{method.lower()}", body=body
                        )
                        payload = response.json()
                        expected = (
                            body.getvalue().decode()
                            if isinstance(body, io.BytesIO)
                            else body.decode()
                        )
                        actual = payload["data"]
                        if actual.startswith("data:"):
                            actual = base64.b64decode(actual.split(",", 1)[1]).decode()
                        self.assertEqual(actual, expected)

    def test_http_lifecycle(self) -> None:
        with PoolManager(resolver=sync_resolver()) as pool:
            response = pool.urlopen("GET", f"{HTTP_URL}/get")
            self.assertEqual(response.status, 200)
            self.assertEqual(response.version, 11)

        pool = PoolManager(resolver=sync_resolver())
        try:
            response = pool.urlopen("GET", f"{HTTP_URL}/get")
            self.assertEqual(response.status, 200)
        finally:
            pool.clear()

    def test_https_conn_info(self) -> None:
        info: ConnectionInfo | None = None

        def on_post_connection(value: ConnectionInfo) -> None:
            nonlocal info
            info = value

        with PoolManager(ca_certs=ROOT_CA, resolver=sync_resolver()) as pool:
            response = pool.urlopen(
                "GET", f"{HTTPS_URL}/get", on_post_connection=on_post_connection
            )
            self.assertEqual(response.status, 200)
            self.assertEqual(response.version, 20)

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.http_version, HttpVersion.h2)
        self.assertIsNotNone(info.certificate_der)
        self.assertIsNotNone(info.cipher)
        self.assertIsNotNone(info.tls_version)
        self.assertIsNotNone(info.destination_address)

        pool = PoolManager(ca_certs=ROOT_CA, resolver=sync_resolver())
        try:
            response = pool.urlopen("GET", f"{HTTPS_URL}/get")
            self.assertEqual(response.status, 200)
        finally:
            pool.clear()

    def test_tls_options_mtls(self) -> None:
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            with PoolManager(cert_reqs=0, resolver=sync_resolver()) as pool:
                response = pool.urlopen("GET", f"{HTTPS_URL}/get")
                self.assertEqual(response.status, 200)
            self.assertTrue(
                any(issubclass(w.category, InsecureRequestWarning) for w in captured)
            )

        with PoolManager(ca_certs=ROOT_CA, assert_hostname=False) as pool:
            response = pool.urlopen("GET", f"{TLS12_URL}/get")
            self.assertEqual(response.status, 200)

        info: ConnectionInfo | None = None

        def on_post_connection(value: ConnectionInfo) -> None:
            nonlocal info
            info = value

        with PoolManager(
            ca_certs=ROOT_CA,
            ssl_maximum_version=ssl.TLSVersion.TLSv1_2,
        ) as pool:
            response = pool.urlopen(
                "GET", f"{TLS12_URL}/get", on_post_connection=on_post_connection
            )
            self.assertEqual(response.status, 200)
        assert info is not None
        self.assertEqual(info.tls_version, ssl.TLSVersion.TLSv1_2)

        with PoolManager(
            ca_certs=ROOT_CA,
            cert_file=CLIENT_CERT,
            key_file=CLIENT_KEY,
        ) as pool:
            response = pool.urlopen("GET", f"{MTLS_URL}/certificate")
            self.assertTrue(response.json()["client_certificate"])

    def test_websocket_sse(self) -> None:
        with PoolManager(ca_certs=ROOT_CA, resolver=sync_resolver()) as pool:
            response = pool.urlopen(
                "GET", HTTPS_URL.replace("https://", "wss://") + "/websocket/echo"
            )
            self.assertEqual(response.status, 101)
            self.assertIsInstance(response.extension, WebSocketExtensionFromHTTP)
            assert response.extension is not None
            response.extension.send_payload("sync wasi")
            response.extension.send_payload(b"sync bytes")
            self.assertEqual(response.extension.next_payload(), "sync wasi")
            self.assertEqual(response.extension.next_payload(), b"sync bytes")
            response.extension.close()

            response = pool.urlopen(
                "GET",
                HTTPS_URL.replace("https://", "sse://") + "/sse?delay=10ms&count=3",
            )
            self.assertIsInstance(response.extension, ServerSideEventExtensionFromHTTP)
            assert response.extension is not None
            events = []
            while not response.extension.closed:
                event = response.extension.next_payload()
                if event is not None:
                    events.append(event)
            self.assertEqual(len(events), 3)

    def test_proxies(self) -> None:
        with proxy_from_url(
            HTTP_PROXY_URL, ca_certs=COMBINED_CA, resolver=sync_resolver()
        ) as pool:
            self.assertEqual(pool.urlopen("GET", f"{HTTP_URL}/get").status, 200)
            self.assertEqual(pool.urlopen("GET", f"{HTTPS_URL}/get").status, 200)

        with proxy_from_url(
            HTTPS_PROXY_URL, ca_certs=COMBINED_CA, resolver=sync_resolver()
        ) as pool:
            self.assertEqual(pool.urlopen("GET", f"{HTTPS_URL}/get").status, 200)

        with SOCKSProxyManager(
            SOCKS_PROXY_URL, ca_certs=ROOT_CA, resolver=sync_resolver()
        ) as pool:
            self.assertEqual(pool.urlopen("GET", f"{HTTPS_URL}/get").status, 200)

    def test_http2_parallel_streams(self) -> None:
        with PoolManager(
            ca_certs=ROOT_CA,
            resolver=sync_resolver(),
            maxsize=1,
            disabled_svn={HttpVersion.h11, HttpVersion.h3},
        ) as pool:
            promises = [
                pool.urlopen("GET", f"{HTTPS_URL}/get?i={index}", multiplexed=True)
                for index in range(3)
            ]
            self.assertTrue(all(promise is not None for promise in promises))
            responses = [pool.get_response() for _ in promises]
            self.assertTrue(all(response is not None for response in responses))
            self.assertTrue(
                all(response.version == 20 for response in responses if response)
            )

    def test_http1_fallback(self) -> None:
        with PoolManager(
            ca_certs=ROOT_CA,
            resolver=sync_resolver(),
            disabled_svn={HttpVersion.h2, HttpVersion.h3},
        ) as pool:
            response = pool.urlopen("GET", f"{HTTPS_URL}/get")
            self.assertEqual(response.version, 11)
