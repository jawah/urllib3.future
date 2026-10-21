from __future__ import annotations

import asyncio
import base64
import io
import errno
import socket
import unittest
import warnings
from typing import Any, cast

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
from urllib3.util import parse_url
from urllib3.contrib.wasi._async import socket as wasi_socket

from ..common import (
    edge_address,
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
            for base_url in (HTTP_URL, HTTPS_URL):
                response = await pool.urlopen(
                    "GET", base_url.replace("http", "ws", 1) + "/websocket/echo"
                )
                self.assertEqual(response.status, 101)
                self.assertIsInstance(
                    response.extension, AsyncWebSocketExtensionFromHTTP
                )
                websocket = cast(AsyncWebSocketExtensionFromHTTP, response.extension)

                async def send() -> None:
                    await websocket.send_payload("async wasi")
                    await websocket.send_payload(b"async bytes")
                    await websocket.ping()

                first, second, _ = await asyncio.gather(
                    websocket.next_payload(), websocket.next_payload(), send()
                )
                self.assertEqual(first, "async wasi")
                self.assertEqual(second, b"async bytes")
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

    async def test_socket_addresses(self) -> None:
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
                records = await wasi_socket.getaddrinfo(host, port, family, flags=flags)
                self.assertEqual(len(records), 2)
                self.assertEqual(
                    {record[1] for record in records},
                    {socket.SOCK_STREAM, socket.SOCK_DGRAM},
                )
                for record in records:
                    self.assertEqual(record[4][:2], (expected_host, expected_port))
        records = await wasi_socket.getaddrinfo(
            "127.0.0.1", 80, type=socket.SOCK_DGRAM, flags=socket.AI_CANONNAME
        )
        self.assertEqual(records[0][3], "127.0.0.1")
        for host, family, flags in [
            ("not-a-number", socket.AF_UNSPEC, socket.AI_NUMERICHOST),
            ("::1", socket.AF_INET, 0),
        ]:
            with self.assertRaises(socket.gaierror):
                await wasi_socket.getaddrinfo(host, 80, family, flags=flags)

    async def test_socket_options(self) -> None:
        # The facade selects a different socket class inside the WASI component.
        with cast(Any, wasi_socket.socket()) as sock:
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

    async def test_socket_partial_reads_and_eof(self) -> None:
        for endpoint in (HTTP_URL, HTTPS_URL):
            with self.subTest(endpoint=endpoint):
                url = parse_url(endpoint)
                assert url.host is not None and url.port is not None
                sock: Any = await wasi_socket.create_connection((url.host, url.port))
                try:
                    if url.scheme == "https":
                        context = ssl.create_default_context(cafile=ROOT_CA)
                        context.set_alpn_protocols(["http/1.1"])
                        sock = await sock.wrap_socket(context, server_hostname=url.host)
                    self.assertFalse(sock.should_connect())
                    self.assertEqual(sock.getpeername()[1], url.port)
                    self.assertGreater(sock.getsockname()[1], 0)
                    sock.settimeout(2)
                    self.assertEqual(sock.gettimeout(), 2)
                    request = f"GET /bytes/64 HTTP/1.1\r\nHost: {url.host}\r\nConnection: close\r\n\r\n".encode()
                    await sock.write_all(request)
                    await sock.until_data_available()
                    data = bytearray(await sock.read_exact(4))
                    self.assertEqual(data, b"HTTP")
                    buffer = bytearray(17)
                    while True:
                        size = await sock.recv_into(buffer)
                        if not size:
                            break
                        data.extend(buffer[:size])
                    head, body = data.split(b"\r\n\r\n", 1)
                    self.assertTrue(head.startswith(b"HTTP/1.1 200"))
                    self.assertEqual(len(body), 64)
                    with self.assertRaises(EOFError):
                        await sock.read_exact(1)
                finally:
                    sock.close()
                    await sock.wait_for_close()

    async def test_socket_invalid_operations(self) -> None:
        for family, kind in (
            (-1, socket.SOCK_STREAM),
            (socket.AF_INET, socket.SOCK_DGRAM),
        ):
            with self.subTest(family=family, kind=kind), self.assertRaises(OSError):
                wasi_socket.socket(family, kind)  # type: ignore[arg-type]
        sock: Any = wasi_socket.socket()
        try:
            for operation in (sock.getsockname, sock.getpeername):
                with self.assertRaises(OSError):
                    operation()
            with self.assertRaises(OSError):
                await sock.recv(1)
            with self.assertRaises(OSError):
                await sock.send(b"x")
            for option in (
                socket.TCP_KEEPIDLE,
                socket.TCP_KEEPINTVL,
                socket.TCP_KEEPCNT,
            ):
                with self.subTest(option=option), self.assertRaises(OSError):
                    sock.setsockopt(socket.IPPROTO_TCP, option, 0)
        finally:
            sock.close()

    async def test_socket_names_and_service_errors(self) -> None:
        records = await wasi_socket.getaddrinfo(
            "localhost", 80, socket.AF_INET, socket.SOCK_STREAM
        )
        self.assertIn("127.0.0.1", [r[4][0] for r in records])
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            with self.assertRaises(OSError):
                await wasi_socket.getaddrinfo(
                    "127.0.0.1", "urllib3-no-such-service", type=kind
                )
        with self.assertRaises(socket.gaierror):
            await wasi_socket.getaddrinfo("x" * 64 + ".invalid", 80)
        with self.assertRaises((ValueError, socket.gaierror)):
            await wasi_socket.getaddrinfo("127.0.0.1", 80, type=-1, proto=17)

    async def test_socket_network_denied(self) -> None:
        address = edge_address("plain")
        assert address is not None
        with self.assertRaises(OSError) as caught:
            await wasi_socket.create_connection(address)
        self.assertEqual(caught.exception.errno, errno.EACCES)

    async def test_socket_dns_denied(self) -> None:
        with self.assertRaises(socket.gaierror) as caught:
            await wasi_socket.getaddrinfo(
                "localhost", 80, socket.AF_INET, socket.SOCK_STREAM
            )
        self.assertEqual(caught.exception.errno, socket.EAI_FAIL)

    async def test_socket_refused_connection(self) -> None:
        address = edge_address("refused")
        assert address is not None
        with self.assertRaises(ConnectionRefusedError):
            await wasi_socket.create_connection(address)

    async def test_socket_ipv6_io(self) -> None:
        address = edge_address("ipv6")
        if address is None:
            self.skipTest("IPv6 loopback is unavailable on the host")
        sock: Any = wasi_socket.socket(socket.AF_INET6)
        try:
            await sock.connect(address)
            self.assertEqual(sock.getpeername()[:2], address)
            self.assertEqual(sock.getsockname()[0], "::1")
            await sock.sendall(b"echo\n")
            data = bytearray()
            while True:
                chunk = await sock.recv(16)
                if not chunk:
                    break
                data.extend(chunk)
            self.assertEqual(bytes(data), b"hello")
        finally:
            sock.close()

    async def test_socket_tls_handshake_eof(self) -> None:
        address = edge_address("handshake-eof")
        assert address is not None
        sock = await wasi_socket.create_connection(address)
        context = ssl.create_default_context(cafile=ROOT_CA)
        try:
            with self.assertRaises(ssl.SSLEOFError):
                await sock.wrap_socket(context, server_hostname="localhost")
        finally:
            sock.close()
            await sock.wait_for_close()

    async def test_socket_tls_metadata(self) -> None:
        url = parse_url(TLS12_URL)
        assert url.host is not None and url.port is not None
        raw: Any = await wasi_socket.create_connection((url.host, url.port))
        context = ssl.create_default_context(cafile=ROOT_CA)
        sock = await raw.wrap_socket(context, server_hostname=url.host)
        try:
            with sock:
                self.assertTrue(sock.getpeercert(binary_form=True))
                self.assertIsNotNone(sock.cipher())
                self.assertEqual(sock.version(), "TLSv1.2")
                await sock.wait_for_readiness()
                request = f"GET /get HTTP/1.1\r\nHost: {url.host}\r\nConnection: close\r\n\r\n".encode()
                size = await sock.send(request)
                await sock.sendall(request[size:])
                await sock.until_data_available()
                self.assertTrue(sock.read_ready())
                self.assertEqual(await sock.read_exact(4), b"HTTP")
                while await sock.recv(4096):
                    pass
        finally:
            sock.close()
            await sock.wait_for_close()
        await sock.wait_for_close()
        self.assertEqual(sock.fileno(), -1)

    async def test_socket_closed_addresses(self) -> None:
        sock: Any = wasi_socket.socket()
        sock.close()
        for operation in (sock.getsockname, sock.getpeername):
            with self.assertRaises(OSError) as caught:
                operation()
            self.assertEqual(caught.exception.errno, errno.EBADF)

    async def test_socket_zero_read_does_not_wait(self) -> None:
        for name in ("plain", "tls"):
            with self.subTest(transport=name):
                address = edge_address(name)
                assert address is not None
                sock: Any = await wasi_socket.create_connection(address)
                try:
                    if name == "tls":
                        context = ssl.create_default_context(cafile=ROOT_CA)
                        sock = await sock.wrap_socket(
                            context, server_hostname="localhost"
                        )
                    await sock.sendall(b"zero-read\n")
                    self.assertEqual(await sock.recv(0), b"")
                    await sock.sendall(b"!")
                    self.assertEqual(await sock.read_exact(5), b"ready")
                finally:
                    sock.close()
                    await sock.wait_for_close()

    async def test_socket_tls_corrupt_record(self) -> None:
        address = edge_address("tls")
        assert address is not None
        raw: Any = await wasi_socket.create_connection(address)
        context = ssl.create_default_context(cafile=ROOT_CA)
        sock = await raw.wrap_socket(context, server_hostname="localhost")
        try:
            await sock.sendall(b"corrupt\n")
            with self.assertRaises(ssl.SSLError):
                await sock.recv(1)
            with self.assertRaises(ssl.SSLError):
                await sock.send(b"late")
        finally:
            sock.close()
            try:
                await sock.wait_for_close()
            except OSError:
                # The malformed-record peer may reject the outgoing TLS alert.
                pass
        self.assertEqual(sock.fileno(), -1)

    async def test_socket_write_after_peer_reset(self) -> None:
        address = edge_address("plain")
        assert address is not None
        sock = await wasi_socket.create_connection(address)
        try:
            await sock.sendall(b"reset\n")
            # P3 reports completion/error separately from the byte stream's EOF.
            try:
                self.assertEqual(await sock.recv(1), b"")
            except OSError:
                pass
            with self.assertRaises(OSError):
                await sock.sendall(b"late")
        finally:
            sock.close()
            await sock.wait_for_close()

    async def test_socket_tls_partial_close(self) -> None:
        url = parse_url(HTTPS_URL)
        assert url.host is not None and url.port is not None
        raw: Any = await wasi_socket.create_connection((url.host, url.port))
        context = ssl.create_default_context(cafile=ROOT_CA)
        context.set_alpn_protocols(["http/1.1"])
        sock = await raw.wrap_socket(context, server_hostname=url.host)
        try:
            request = f"GET /bytes/262144 HTTP/1.1\r\nHost: {url.host}\r\nConnection: close\r\n\r\n".encode()
            await sock.sendall(request)
            await sock.until_data_available()
            self.assertEqual(await sock.recv(1), b"H")
        finally:
            sock.close()
            await sock.wait_for_close()
        self.assertEqual(sock.fileno(), -1)
