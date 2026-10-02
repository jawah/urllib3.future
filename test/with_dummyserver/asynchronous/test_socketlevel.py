from __future__ import annotations

import asyncio
import ssl
import typing

import pytest
from jh2.config import H2Configuration  # type: ignore[import-untyped]
from jh2.connection import H2Connection  # type: ignore[import-untyped]
from jh2.events import RequestReceived  # type: ignore[import-untyped]
from urllib3 import HttpVersion, ResponsePromise, AsyncProxyManager
from urllib3 import AsyncHTTPConnectionPool, AsyncHTTPSConnectionPool
from urllib3.exceptions import (
    IncompleteRead,
    MaxRetryError,
    InvalidHeader,
    ProtocolError,
)

from dummyserver.server import DEFAULT_CA, DEFAULT_CERTS
from dummyserver.testcase import SocketDummyServerTestCase, consume_socket
from threading import Event
import socket


@pytest.mark.asyncio
class TestSocketClosing(SocketDummyServerTestCase):
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
