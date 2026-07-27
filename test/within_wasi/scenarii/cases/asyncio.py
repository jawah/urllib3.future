from __future__ import annotations

import asyncio
import base64
import io
import unittest
import warnings
from typing import cast

from urllib3 import (
    AsyncPoolManager,
    ConnectionInfo,
    HttpVersion,
    async_proxy_from_url,
)
from urllib3.contrib.socks import AsyncSOCKSProxyManager
from urllib3.contrib.anytls import ssl
from urllib3.contrib.webextensions._async import (
    AsyncServerSideEventExtensionFromHTTP,
)
from urllib3.contrib.webextensions._async.ws import AsyncWebSocketExtensionFromHTTP
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
    async_resolver,
)


class AsyncWasiTests(unittest.TestCase):
    __test__ = False

    async def test_methods_bodies(self) -> None:
        for base_url, ca_certs in ((HTTP_URL, None), (HTTPS_URL, ROOT_CA)):
            bodies: dict[str, bytes | io.BytesIO] = {
                "POST": b"post bytes",
                "PUT": io.BytesIO(b"put BytesIO"),
                "PATCH": io.BytesIO(b"patch BytesIO"),
            }
            async with AsyncPoolManager(
                ca_certs=ca_certs, resolver=async_resolver()
            ) as pool:
                for method in ("GET", "DELETE"):
                    with self.subTest(url=base_url, method=method):
                        response = await pool.urlopen(
                            method, f"{base_url}/{method.lower()}"
                        )
                        self.assertEqual(response.status, 200)

                for method, body in bodies.items():
                    with self.subTest(
                        url=base_url, method=method, body=type(body).__name__
                    ):
                        response = await pool.urlopen(
                            method, f"{base_url}/{method.lower()}", body=body
                        )
                        payload = await response.json()
                        expected = (
                            body.getvalue().decode()
                            if isinstance(body, io.BytesIO)
                            else body.decode()
                        )
                        actual = payload["data"]
                        if actual.startswith("data:"):
                            actual = base64.b64decode(actual.split(",", 1)[1]).decode()
                        self.assertEqual(actual, expected)

    async def test_http_lifecycle(self) -> None:
        async with AsyncPoolManager(resolver=async_resolver()) as pool:
            response = await pool.urlopen("GET", f"{HTTP_URL}/get")
            self.assertEqual(response.status, 200)
            self.assertEqual(response.version, 11)

        pool = AsyncPoolManager(resolver=async_resolver())
        try:
            response = await pool.urlopen("GET", f"{HTTP_URL}/get")
            self.assertEqual(response.status, 200)
        finally:
            await pool.clear()

    async def test_https_conn_info(self) -> None:
        info: ConnectionInfo | None = None

        async def on_post_connection(value: ConnectionInfo) -> None:
            nonlocal info
            info = value

        async with AsyncPoolManager(
            ca_certs=ROOT_CA, resolver=async_resolver()
        ) as pool:
            response = await pool.urlopen(
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

        pool = AsyncPoolManager(ca_certs=ROOT_CA, resolver=async_resolver())
        try:
            response = await pool.urlopen("GET", f"{HTTPS_URL}/get")
            self.assertEqual(response.status, 200)
        finally:
            await pool.clear()

    async def test_tls_options_mtls(self) -> None:
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            async with AsyncPoolManager(cert_reqs=0, resolver=async_resolver()) as pool:
                response = await pool.urlopen("GET", f"{HTTPS_URL}/get")
                self.assertEqual(response.status, 200)
            self.assertTrue(
                any(issubclass(w.category, InsecureRequestWarning) for w in captured)
            )

        async with AsyncPoolManager(ca_certs=ROOT_CA, assert_hostname=False) as pool:
            response = await pool.urlopen("GET", f"{TLS12_URL}/get")
            self.assertEqual(response.status, 200)

        info: ConnectionInfo | None = None

        async def on_post_connection(value: ConnectionInfo) -> None:
            nonlocal info
            info = value

        async with AsyncPoolManager(
            ca_certs=ROOT_CA,
            ssl_maximum_version=ssl.TLSVersion.TLSv1_2,
        ) as pool:
            response = await pool.urlopen(
                "GET", f"{TLS12_URL}/get", on_post_connection=on_post_connection
            )
            self.assertEqual(response.status, 200)
        assert info is not None
        self.assertEqual(info.tls_version, ssl.TLSVersion.TLSv1_2)

        async with AsyncPoolManager(
            ca_certs=ROOT_CA,
            cert_file=CLIENT_CERT,
            key_file=CLIENT_KEY,
        ) as pool:
            response = await pool.urlopen("GET", f"{MTLS_URL}/certificate")
            self.assertTrue((await response.json())["client_certificate"])

    async def test_websocket_sse(self) -> None:
        async with AsyncPoolManager(
            ca_certs=ROOT_CA, resolver=async_resolver()
        ) as pool:
            response = await pool.urlopen(
                "GET", HTTPS_URL.replace("https://", "wss://") + "/websocket/echo"
            )
            self.assertEqual(response.status, 101)
            self.assertIsInstance(response.extension, AsyncWebSocketExtensionFromHTTP)
            websocket = cast(AsyncWebSocketExtensionFromHTTP, response.extension)
            await websocket.send_payload("async wasi")
            await websocket.send_payload(b"async bytes")
            await websocket.ping()
            self.assertEqual(await websocket.next_payload(), "async wasi")
            self.assertEqual(await websocket.next_payload(), b"async bytes")
            await websocket.close()

            response = await pool.urlopen(
                "GET",
                HTTPS_URL.replace("https://", "sse://") + "/sse?delay=10ms&count=3",
            )
            self.assertIsInstance(
                response.extension, AsyncServerSideEventExtensionFromHTTP
            )
            assert response.extension is not None
            events = []
            while not response.extension.closed:
                event = await response.extension.next_payload()
                if event is not None:
                    events.append(event)
            self.assertEqual(len(events), 3)

    async def test_proxies(self) -> None:
        async with async_proxy_from_url(
            HTTP_PROXY_URL, ca_certs=COMBINED_CA, resolver=async_resolver()
        ) as pool:
            self.assertEqual((await pool.urlopen("GET", f"{HTTP_URL}/get")).status, 200)
            self.assertEqual(
                (await pool.urlopen("GET", f"{HTTPS_URL}/get")).status, 200
            )

        async with async_proxy_from_url(
            HTTPS_PROXY_URL, ca_certs=COMBINED_CA, resolver=async_resolver()
        ) as pool:
            self.assertEqual(
                (await pool.urlopen("GET", f"{HTTPS_URL}/get")).status, 200
            )

        async with AsyncSOCKSProxyManager(
            SOCKS_PROXY_URL, ca_certs=ROOT_CA, resolver=async_resolver()
        ) as pool:
            self.assertEqual(
                (await pool.urlopen("GET", f"{HTTPS_URL}/get")).status, 200
            )

    async def test_http2_parallel_streams(self) -> None:
        async with AsyncPoolManager(
            ca_certs=ROOT_CA,
            resolver=async_resolver(),
            maxsize=1,
            disabled_svn={HttpVersion.h11, HttpVersion.h3},
        ) as pool:
            promises = [
                await pool.urlopen(
                    "GET", f"{HTTPS_URL}/get?i={index}", multiplexed=True
                )
                for index in range(3)
            ]
            responses = [
                await pool.get_response(promise=promise) for promise in promises
            ]
            self.assertTrue(all(response is not None for response in responses))
            self.assertTrue(
                all(response.version == 20 for response in responses if response)
            )

    async def test_http1_fallback(self) -> None:
        async with AsyncPoolManager(
            ca_certs=ROOT_CA,
            resolver=async_resolver(),
            disabled_svn={HttpVersion.h2, HttpVersion.h3},
        ) as pool:
            response = await pool.urlopen("GET", f"{HTTPS_URL}/get")
            self.assertEqual(response.version, 11)

    async def test_concurrency(self) -> None:
        async with AsyncPoolManager(
            ca_certs=ROOT_CA,
            resolver=async_resolver(),
            maxsize=10,
        ) as pool:
            responses = await asyncio.gather(
                *(
                    pool.urlopen("GET", f"{HTTPS_URL}/get?i={index}")
                    for index in range(32)
                )
            )
            self.assertEqual(len(responses), 32)
            self.assertTrue(all(response.status == 200 for response in responses))
            self.assertTrue(all(response.version == 20 for response in responses))
