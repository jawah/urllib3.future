from __future__ import annotations

import base64
import io
import socket
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
from urllib3.util import parse_url
from urllib3.contrib.wasi import socket as wasi_socket

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

    def test_socket_addresses(self) -> None:
        cases: list[
            tuple[bytes | str | None, bytes | str | int | None, int, int, str, int]
        ] = [
            (b"127.0.0.1", b"80", socket.AF_UNSPEC, 0, "127.0.0.1", 80),
            ("::1", None, socket.AF_UNSPEC, 0, "::1", 0),
            (None, "443", socket.AF_INET, 0, "127.0.0.1", 443),
            (None, 80, socket.AF_INET6, socket.AI_PASSIVE, "::", 80),
        ]
        for host, port, family, flags, expected_host, expected_port in cases:
            with self.subTest(host=host, port=port, flags=flags):
                records = wasi_socket.getaddrinfo(host, port, family, flags=flags)
                self.assertEqual(len(records), 2)
                self.assertEqual(
                    {record[1] for record in records},
                    {socket.SOCK_STREAM, socket.SOCK_DGRAM},
                )
                for record in records:
                    self.assertEqual(record[4][:2], (expected_host, expected_port))
        records = wasi_socket.getaddrinfo(
            "127.0.0.1", 80, type=socket.SOCK_DGRAM, flags=socket.AI_CANONNAME
        )
        self.assertEqual(records[0][3], "127.0.0.1")
        for host, family, flags in [
            ("not-a-number", socket.AF_UNSPEC, socket.AI_NUMERICHOST),
            ("::1", socket.AF_INET, 0),
        ]:
            with self.assertRaises(socket.gaierror):
                wasi_socket.getaddrinfo(host, 80, family, flags=flags)

    def test_socket_options(self) -> None:
        with wasi_socket.socket() as sock:
            self.assertIsNone(sock.gettimeout())
            sock.settimeout(2)
            self.assertEqual(sock.gettimeout(), 2)
            with self.assertRaises(ValueError):
                sock.settimeout(-1)
            for level, name, value in [
                (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
                (socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30),
                (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5),
                (socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3),
            ]:
                with self.subTest(option=name):
                    sock.setsockopt(level, name, value)
                    self.assertEqual(sock.getsockopt(level, name), value)
            for name in (socket.SO_SNDBUF, socket.SO_RCVBUF):
                sock.setsockopt(socket.SOL_SOCKET, name, 16384)
                self.assertGreaterEqual(sock.getsockopt(socket.SOL_SOCKET, name), 16384)
            self.assertEqual(sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR), 0)
            with self.assertRaises(OSError):
                sock.getsockopt(socket.SOL_SOCKET, -1)
            with self.assertRaises(OSError):
                sock.setsockopt(socket.SOL_SOCKET, -1, 1)
        sock.close()
        self.assertEqual(sock.fileno(), -1)
        with self.assertRaises(OSError):
            sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
        with self.assertRaises(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

    def test_socket_partial_reads_and_eof(self) -> None:
        url = parse_url(HTTP_URL)
        with wasi_socket.create_connection((url.host, url.port), timeout=2) as sock:
            self.assertEqual(sock.getpeername()[1], url.port)
            self.assertGreater(sock.getsockname()[1], 0)
            self.assertEqual(sock.gettimeout(), 2)
            sock.setblocking(False)
            self.assertFalse(sock.getblocking())
            sock.setblocking(True)
            self.assertTrue(sock.getblocking())
            request = f"GET /bytes/64 HTTP/1.1\r\nHost: {url.host}\r\nConnection: close\r\n\r\n".encode()
            self.assertEqual(sock.send(b""), 0)
            sock.sendall(request)
            self.assertEqual(sock.recv(0), b"")
            data = bytearray()
            buffer = bytearray(17)
            while True:
                size = sock.recv_into(buffer)
                if not size:
                    break
                data.extend(buffer[:size])
            head, body = data.split(b"\r\n\r\n", 1)
            self.assertTrue(head.startswith(b"HTTP/1.1 200"))
            self.assertEqual(len(body), 64)
            self.assertEqual(sock.recv(1), b"")
