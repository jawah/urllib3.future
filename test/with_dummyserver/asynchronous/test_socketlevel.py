import asyncio
import time

import pytest
from urllib3 import AsyncHTTPConnectionPool, HttpVersion
from urllib3.exceptions import IncompleteRead, InvalidHeader, ProtocolError

from dummyserver.testcase import SocketDummyServerTestCase, goaway_on_idle_handler
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
class TestReuseAfterGoaway(SocketDummyServerTestCase):
    """Async mirror of the sync test of the same name in
    ``test/with_dummyserver/test_socketlevel.py``."""

    async def test_idle_connection_that_received_goaway_is_not_reused(self) -> None:
        accepted: list[socket.socket] = []
        self._start_server(goaway_on_idle_handler(0.5, 2.0, accepted))

        async with AsyncHTTPConnectionPool(
            self.host, self.port, retries=False, disabled_svn={HttpVersion.h11}
        ) as pool:
            assert (await pool.request("POST", "/", body=b"{}")).status == 200
            await asyncio.sleep(1.2)
            assert (await pool.request("POST", "/", body=b"{}")).status == 200

        assert len(accepted) == 2

    @staticmethod
    def _stale_408_handler(listener: socket.socket) -> None:
        # Answers one request, then sends an unsolicited 408 while the
        # connection is idle and closes it only 2s later.
        sock = listener.accept()[0]
        buf = b""
        while not buf.endswith(b"\r\n\r\n"):
            buf += sock.recv(65536)
        sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        time.sleep(0.5)
        sock.sendall(
            b"HTTP/1.1 408 Request Timeout\r\n"
            b"Content-Length: 0\r\nConnection: close\r\n\r\n"
        )
        time.sleep(2.0)
        sock.close()

    async def test_idle_http11_connection_is_not_read_before_reuse(self) -> None:
        self._start_server(self._stale_408_handler)

        async with AsyncHTTPConnectionPool(self.host, self.port, retries=False) as pool:
            assert (await pool.request("GET", "/")).status == 200
            await asyncio.sleep(1.2)
            assert (await pool.request("GET", "/")).status == 408
