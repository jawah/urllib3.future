from __future__ import annotations

import os
import asyncio
import ssl
import typing

import pytest
from jh2.config import H2Configuration  # type: ignore[import-untyped]
from jh2.connection import H2Connection  # type: ignore[import-untyped]
from jh2.events import RequestReceived  # type: ignore[import-untyped]
from urllib3 import HttpVersion, ResponsePromise, AsyncProxyManager
from urllib3 import AsyncHTTPConnectionPool, AsyncHTTPSConnectionPool, AsyncPoolManager
from urllib3.contrib.webextensions._async.raw import AsyncRawExtensionFromHTTP
from urllib3.contrib.webextensions._async.sse import (
    AsyncServerSideEventExtensionFromHTTP,
)
from urllib3.contrib.webextensions.sse import ServerSentEvent
from urllib3.exceptions import (
    IncompleteRead,
    MaxRetryError,
    InvalidHeader,
    ProtocolError,
    ReadTimeoutError,
)

from dummyserver.server import DEFAULT_CA, DEFAULT_CERTS
from dummyserver.testcase import SocketDummyServerTestCase, consume_socket
from threading import Event
import socket


@pytest.mark.asyncio
class TestSSL(SocketDummyServerTestCase):
    @pytest.mark.parametrize(
        "content_length,preload_content,read_amt",
        [
            pytest.param(8193, False, 2**31, id="oversized-read-small-body"),
            *[
                pytest.param(
                    2**31,
                    preload_content,
                    read_amt,
                    marks=pytest.mark.skipif(
                        os.environ.get("CI") is not None,
                        reason="Run the 2 GiB cases in test_ssl_large_resources",
                    ),
                )
                for preload_content, read_amt in (
                    (True, None),
                    (False, None),
                    (False, 2**31),
                )
            ],
        ],
    )
    async def test_requesting_large_resources_via_ssl(
        self, content_length: int, preload_content: bool, read_amt: int | None
    ) -> None:
        # Async counterpart of TestSSL.test_requesting_large_resources_via_ssl.
        # A small body with an oversized read also exercises the Python <3.10 guard.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(DEFAULT_CERTS["certfile"], DEFAULT_CERTS["keyfile"])

        def socket_handler(listener: socket.socket) -> None:
            listener.settimeout(30)
            with listener.accept()[0] as sock:
                sock.settimeout(30)
                with context.wrap_socket(sock, server_side=True) as ssl_sock:
                    with ssl_sock.makefile("rb") as requests:
                        for length in (content_length, 5):
                            while True:
                                line = requests.readline()
                                if not line:
                                    return  # The client may close after a failed assertion.
                                if line == b"\r\n":
                                    break
                            ssl_sock.sendall(
                                b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
                                + f"Content-Length: {length}\r\n\r\n".encode()
                            )
                            # Reuse a small chunk instead of allocating another large body.
                            chunk = b"Hello" if length == 5 else b"x" * 65536
                            for offset in range(0, length, len(chunk)):
                                ssl_sock.sendall(
                                    chunk[: min(len(chunk), length - offset)]
                                )

        self._start_server(socket_handler)
        async with AsyncHTTPSConnectionPool(
            self.host, self.port, ca_certs=DEFAULT_CA, retries=False, timeout=30
        ) as pool:
            response = await pool.request("GET", "/", preload_content=preload_content)
            assert response.status == 200
            if not preload_content:
                assert await response.read(0) == b""
            data = (
                await response.data
                if preload_content
                else await response.read(read_amt)
            )
            assert len(data) == content_length
            assert data.count(b"x") == content_length
            del data

            buffer = bytearray(b"untouched")
            assert await response.readinto(buffer) == 0
            assert buffer == b"untouched"
            response = await pool.request("GET", "/again")
            assert response.status == 200
            assert await response.data == b"Hello"
            assert pool.num_connections == 1


@pytest.mark.asyncio
class TestRawExtension(SocketDummyServerTestCase):
    @pytest.mark.parametrize("message", ["hello 🚀", b"hello\x00world"])
    async def test_timeout_then_echo(self, message: str | bytes) -> None:
        expected = message.encode() if isinstance(message, str) else message

        def socket_handler(listener: socket.socket) -> None:
            with listener.accept()[0] as sock:
                sock.settimeout(5)
                consume_socket(sock)
                sock.sendall(
                    b"HTTP/1.1 101 Switching Protocols\r\n"
                    b"Connection: Upgrade\r\nUpgrade: echo\r\n\r\n"
                )
                data = b""
                while len(data) < len(expected):
                    chunk = sock.recv(65536)
                    assert chunk
                    data += chunk
                assert data == expected
                sock.sendall(data)
                assert sock.recv(1) == b""

        self._start_server(socket_handler)
        async with AsyncPoolManager(timeout=5) as manager:
            response = await manager.urlopen(
                "GET",
                f"http://{self.host}:{self.port}/",
                headers={"Connection": "Upgrade", "Upgrade": "echo"},
                extension=AsyncRawExtensionFromHTTP(),
            )
            assert response.status == 101
            extension = response.extension
            assert isinstance(extension, AsyncRawExtensionFromHTTP)
            assert response._police_officer is not None
            async with response._police_officer.borrow(response) as conn:
                assert conn.sock is not None
                conn.sock.settimeout(0.05)
            # The peer sends nothing until we write, so this timeout is deterministic.
            with pytest.raises(ReadTimeoutError):
                await extension.next_payload()
            assert not extension.closed
            async with response._police_officer.borrow(response) as conn:
                assert conn.sock is not None
                conn.sock.settimeout(5)
            await extension.send_payload(message)
            received = b""
            while len(received) < len(expected):
                chunk = await extension.next_payload()
                assert chunk
                received += chunk
            assert received == expected
            await extension.close()
            await extension.close()
            assert extension.closed
            with pytest.raises(OSError, match="closed"):
                await extension.next_payload()
            with pytest.raises(OSError, match="closed"):
                await extension.send_payload(message)


@pytest.mark.asyncio
class TestServerSentEvents(SocketDummyServerTestCase):
    async def test_extension_request_headers(self) -> None:
        extension_request_headers = {
            "Accept": "text/event-stream; charset=utf-8",
            "X-Client": "preserved",
        }
        received = []

        def handler(listener: socket.socket) -> None:
            with listener.accept()[0] as sock:
                sock.settimeout(5)
                headers = bytearray()
                while not headers.endswith(b"\r\n\r\n"):
                    data = sock.recv(65536)
                    assert data
                    headers.extend(data)
                received.append(bytes(headers))
                sock.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Content-Length: 10\r\n\r\ndata: ok\n\n"
                )

        self._start_server(handler)
        async with AsyncHTTPConnectionPool(self.host, self.port, timeout=5) as pool:
            response = await pool.urlopen(
                "GET",
                "/",
                headers=extension_request_headers,
                extension=AsyncServerSideEventExtensionFromHTTP(),
                preload_content=False,
            )
            assert response.extension is not None
            event = await response.extension.next_payload()
            assert isinstance(event, ServerSentEvent) and event.data == "ok"
            await response.extension.close()
        assert b"Accept: text/event-stream; charset=utf-8\r\n" in received[0]
        assert b"X-Client: preserved\r\n" in received[0]
        assert dict(extension_request_headers) == {
            "Accept": "text/event-stream; charset=utf-8",
            "X-Client": "preserved",
        }

    @pytest.mark.parametrize("blocksize", [1, 65536])
    @pytest.mark.parametrize("raw", [False, True])
    async def test_event_fields_and_boundaries(self, blocksize: int, raw: bool) -> None:
        events = [
            'event: update\r\nid: cursor-1\r\nretry: 1500\r\ndata: {"text": "🚀"}\r\n\r\n',
            "unknown: ignored\nid: bad\x00id\nretry: soon\ndata: second\n\n",
            "data:third\n\n",
        ]
        body = (": heartbeat\r\n\r\n" + "".join(events)).encode()
        self.start_response_handler(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )

        # One-byte reads also split UTF-8 code points and event separators.
        async with AsyncPoolManager(blocksize=blocksize, timeout=5) as manager:
            response = await manager.request("GET", f"psse://{self.host}:{self.port}/")
            extension = response.extension
            assert isinstance(extension, AsyncServerSideEventExtensionFromHTTP)
            if raw:
                assert [
                    await extension.next_payload(raw=True) for _ in events
                ] == events
            else:
                first, second, third = [await extension.next_payload() for _ in events]
                assert isinstance(first, ServerSentEvent)
                assert isinstance(second, ServerSentEvent)
                assert isinstance(third, ServerSentEvent)
                assert first.event == "update"
                assert first.json() == {"text": "🚀"}
                assert first.retry == 1500
                assert first.id == second.id == third.id == "cursor-1"
                assert second.event == third.event == "message"
                assert second.data == "second"
                assert third.data == "third"
                assert second.retry is None
                assert "retry=1500" in repr(first)
            assert await extension.next_payload() is None
            assert extension.closed
            await extension.close()
            await extension.close()
            with pytest.raises(OSError, match="closed"):
                await extension.next_payload()


@pytest.mark.asyncio
class TestSocketClosing(SocketDummyServerTestCase):
    async def test_idle_tls_close_notify_before_reuse(self) -> None:
        close_first = Event()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # Keep post-handshake TLS 1.3 tickets out of the readiness check.
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(DEFAULT_CERTS["certfile"], DEFAULT_CERTS["keyfile"])

        def handler(listener: socket.socket) -> None:
            listener.settimeout(5)
            for i in range(2):
                with listener.accept()[0] as raw:
                    raw.settimeout(5)
                    with context.wrap_socket(raw, server_side=True) as sock:
                        request = bytearray()
                        while not request.endswith(b"\r\n\r\n"):
                            data = sock.recv(65536)
                            assert data
                            request.extend(data)
                        sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                        if i == 0:
                            assert close_first.wait(5)
                        # Send close_notify without closing TCP; wait for the client.
                        try:
                            sock.unwrap().close()
                        except (ssl.SSLError, ConnectionResetError):
                            pass

        self._start_server(handler)
        async with AsyncHTTPSConnectionPool(
            self.host,
            self.port,
            # Exercise stdlib TLS EOF, including when an alternative is installed.
            ssl_backend="ssl",
            ca_certs=DEFAULT_CA,
            timeout=5,
            background_watch_delay=None,
            retries=False,
        ) as pool:
            assert (await pool.request("GET", "/")).status == 200
            close_first.set()
            assert pool.pool is not None
            async with pool.pool.borrow() as conn:
                previous = conn.sock
                assert previous is not None
                await previous.until_data_available(5)
            response = await pool.request("GET", "/")
            assert (await response.data) == b"ok"
            async with pool.pool.borrow() as conn:
                assert conn.sock is not None and conn.sock is not previous

    async def test_recovery_when_server_closes_connection(self) -> None:
        # Does the pool work seamlessly if an open connection in the
        # connection pool gets hung up on by the server, then reaches
        # the front of the queue again?

        done_closing = Event()

        def socket_handler(listener: socket.socket) -> None:
            for i in 0, 1:
                sock = listener.accept()[0]

                buf = b""
                while not buf.endswith(b"\r\n\r\n"):
                    buf = sock.recv(65536)

                body = f"Response {int(i)}"
                sock.send(
                    (
                        "HTTP/1.1 200 OK\r\n"
                        "Content-Type: text/plain\r\n"
                        "Content-Length: %d\r\n"
                        "\r\n"
                        "%s" % (len(body), body)
                    ).encode("utf-8")
                )

                sock.close()  # simulate a server timing out, closing socket
                done_closing.set()  # let the test know it can proceed

        self._start_server(socket_handler)
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            response = await pool.request("GET", "/", retries=0)
            assert response.status == 200
            assert (await response.data) == b"Response 0"

            done_closing.wait()  # wait until the socket in our pool gets closed

            response = await pool.request("GET", "/", retries=0)
            assert response.status == 200
            assert (await response.data) == b"Response 1"


@pytest.mark.asyncio
class TestRemoteClosedWithoutResponse(SocketDummyServerTestCase):
    """Async mirror of the sync test of the same name in
    ``test/with_dummyserver/test_socketlevel.py``. Exercises the
    ``"Remote end closed connection without response"`` raise in
    ``src/urllib3/backend/_async/hface.py`` ``__exchange_until``.
    """

    @pytest.mark.parametrize(
        "error_code, multiplexed",
        [(0xD, False), (0x8, False), (0x8, True)],
    )
    async def test_http2_peer_reset(
        self,
        error_code: int,
        multiplexed: bool,
    ) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(DEFAULT_CERTS["certfile"], DEFAULT_CERTS["keyfile"])
        context.set_alpn_protocols(["h2"])

        def handler(listener: socket.socket) -> None:
            with listener.accept()[0] as raw:
                raw.settimeout(5)
                with context.wrap_socket(raw, server_side=True) as sock:
                    h2 = H2Connection(config=H2Configuration(client_side=False))
                    h2.initiate_connection()
                    sock.sendall(h2.data_to_send())
                    received = False
                    while not received:
                        data = sock.recv(65536)
                        assert data
                        for event in h2.receive_data(data):
                            if isinstance(event, RequestReceived):
                                h2.reset_stream(event.stream_id, error_code=error_code)
                                received = True
                        sock.sendall(h2.data_to_send())

        self._start_server(handler)
        async with AsyncHTTPSConnectionPool(
            self.host,
            self.port,
            ca_certs=DEFAULT_CA,
            timeout=5,
            disabled_svn={HttpVersion.h11, HttpVersion.h3},
            retries=False,
        ) as pool:
            with pytest.raises(ProtocolError, match="reset by remote peer"):
                if multiplexed:
                    result = await pool.urlopen("GET", "/", multiplexed=True)
                    assert isinstance(result, ResponsePromise)
                    await pool.get_response()
                else:
                    await pool.urlopen("GET", "/")

    async def test_http2_rejects_upload_before_body_is_consumed(self) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(DEFAULT_CERTS["certfile"], DEFAULT_CERTS["keyfile"])
        context.set_alpn_protocols(["h2"])
        finished = Event()
        produced = []

        def body() -> typing.Iterator[bytes]:
            for i in range(32):
                produced.append(i)
                yield b"x" * 16384

        def handler(listener: socket.socket) -> None:
            try:
                with listener.accept()[0] as raw:
                    raw.settimeout(5)
                    with context.wrap_socket(raw, server_side=True) as sock:
                        h2 = H2Connection(config=H2Configuration(client_side=False))
                        h2.initiate_connection()
                        sock.sendall(h2.data_to_send())
                        received = False
                        while not received:
                            data = sock.recv(65536)
                            assert data
                            for event in h2.receive_data(data):
                                if isinstance(event, RequestReceived):
                                    h2.send_headers(
                                        event.stream_id,
                                        [(":status", "413"), ("content-length", "0")],
                                        end_stream=True,
                                    )
                                    received = True
                            sock.sendall(h2.data_to_send())
                        # Drain without granting further HTTP/2 flow-control credit.
                        # The client must notice the response and stop its upload.
                        try:
                            while sock.recv(65536):
                                pass
                        except (ssl.SSLError, ConnectionResetError):
                            pass
            finally:
                finished.set()

        self._start_server(handler)
        async with AsyncHTTPSConnectionPool(
            self.host,
            self.port,
            ca_certs=DEFAULT_CA,
            timeout=5,
            disabled_svn={HttpVersion.h11, HttpVersion.h3},
            retries=False,
        ) as pool:
            response = await pool.request("POST", "/", body=body())
            assert response.status == 413 and response.version == 20
            assert (await response.data) == b""
            assert 0 < len(produced) < 32
        assert await asyncio.get_running_loop().run_in_executor(None, finished.wait, 5)

    async def test_proxy_connect_rejected(self) -> None:
        def handler(listener: socket.socket) -> None:
            with listener.accept()[0] as sock:
                sock.settimeout(5)
                request = bytearray()
                while not request.endswith(b"\r\n\r\n"):
                    data = sock.recv(65536)
                    assert data
                    request.extend(data)
                assert request.startswith(b"CONNECT target.invalid:443 ")
                sock.sendall(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")

        self._start_server(handler)
        async with AsyncProxyManager(
            f"http://{self.host}:{self.port}", timeout=5
        ) as proxy:
            with pytest.raises(
                MaxRetryError, match="Tunnel connection failed: 401 Unauthorized"
            ):
                await proxy.request("GET", "https://target.invalid/", retries=0)

    async def test_server_closes_socket_before_status_line(self) -> None:
        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += sock.recv(65536)
            sock.close()

        self._start_server(socket_handler)
        async with AsyncHTTPConnectionPool(self.host, self.port, retries=False) as pool:
            with pytest.raises(
                ProtocolError, match="Remote end closed connection without response"
            ):
                await pool.request("GET", "/")


@pytest.mark.asyncio
class TestInvalidHTTPResponse(SocketDummyServerTestCase):
    """Async mirror covering the malformed-header path."""

    async def test_garbage_header_separator_raises_invalid_header(self) -> None:
        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += sock.recv(65536)
            sock.sendall(
                b"HTTP/1.1 200 OK\r\nNoColonHeaderLine\r\nContent-Length: 0\r\n\r\n"
            )
            sock.close()

        self._start_server(socket_handler)
        async with AsyncHTTPConnectionPool(self.host, self.port, retries=False) as pool:
            with pytest.raises((InvalidHeader, ProtocolError)):
                await pool.request("GET", "/")


@pytest.mark.asyncio
class TestPartialBodyClose(SocketDummyServerTestCase):
    """Async mirror covering the partial-body close path."""

    async def test_server_closes_after_partial_body(self) -> None:
        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += sock.recv(65536)
            sock.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Length: 50\r\n"
                b"Content-Type: text/plain\r\n"
                b"\r\n"
                b"0123456789"
            )
            sock.close()

        self._start_server(socket_handler)
        async with AsyncHTTPConnectionPool(self.host, self.port, retries=False) as pool:
            resp = await pool.request("GET", "/", preload_content=False, retries=False)
            with pytest.raises((IncompleteRead, ProtocolError)):
                await resp.read()


@pytest.mark.asyncio
class TestResponseReadEdges(SocketDummyServerTestCase):
    @pytest.mark.parametrize("decode_content", [False, True])
    async def test_zero_sized_read1_does_not_wait_for_body(
        self, decode_content: bool
    ) -> None:
        send_body = Event()

        def socket_handler(listener: socket.socket) -> None:
            with listener.accept()[0] as sock:
                consume_socket(sock)
                sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n")
                if send_body.wait(10):
                    sock.sendall(b"body")

        self._start_server(socket_handler)
        async with AsyncHTTPConnectionPool(self.host, self.port, timeout=5) as pool:
            response = await pool.urlopen("GET", "/", preload_content=False)
            try:
                # The server sends the body only after the zero-byte read returns.
                assert await response.read1(0, decode_content=decode_content) == b""
                assert response.tell() == 0
                assert response.length_remaining == 4
                send_body.set()
                assert await response.read(cache_content=True) == b"body"
                assert await response.data == b"body"
            finally:
                send_body.set()
                await response.close()

    async def test_early_hints_without_callback(self) -> None:
        self.start_response_handler(
            b"HTTP/1.1 103 Early Hints\r\nLink: </asset>; rel=preload\r\n\r\n"
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
        )
        async with AsyncHTTPConnectionPool(self.host, self.port, timeout=5) as pool:
            response = await pool.urlopen("GET", "/")
            assert response.status == 200
            assert await response.data == b"ok"

    async def test_oversized_read1_from_transport(self) -> None:
        self.start_response_handler(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nbody")
        async with AsyncHTTPConnectionPool(self.host, self.port, timeout=5) as pool:
            response = await pool.urlopen("GET", "/", preload_content=False)
            assert await response.read1(2**31) == b"body"
            assert await response.read1() == b""

    @pytest.mark.parametrize("chunked", [False, True])
    async def test_small_reads_of_compressed_transport(self, chunked: bool) -> None:
        import gzip

        payload = b"decoded body" * 100
        encoded = gzip.compress(payload)
        framing = (
            b"Transfer-Encoding: chunked\r\n"
            if chunked
            else f"Content-Length: {len(encoded)}\r\n".encode()
        )
        wire = (
            f"{len(encoded):x}\r\n".encode() + encoded + b"\r\n0\r\n\r\n"
            if chunked
            else encoded
        )
        self.start_response_handler(
            b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n" + framing + b"\r\n" + wire
        )
        async with AsyncHTTPConnectionPool(
            self.host, self.port, timeout=5, blocksize=1
        ) as pool:
            response = await pool.urlopen("GET", "/", preload_content=False)
            if chunked:
                received = b"".join([part async for part in response.read_chunked(-1)])
            else:
                assert await response.read(1) == payload[:1]
                received = payload[:1] + await response.read()
            assert received == payload

    async def test_sse_leading_empty_line(self) -> None:
        body = b"\rdata: first\n\n"
        self.start_response_handler(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        assert AsyncServerSideEventExtensionFromHTTP.implementation() == "native"
        async with AsyncPoolManager(timeout=5) as manager:
            response = await manager.urlopen("GET", f"psse://{self.host}:{self.port}/")
            assert response.extension is not None
            event = await response.extension.next_payload()
            assert isinstance(event, ServerSentEvent)
            assert event.data == "first"
            await response.extension.close()
            await response.close()
