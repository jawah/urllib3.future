import pytest
from urllib3 import AsyncHTTPConnectionPool

from dummyserver.testcase import SocketDummyServerTestCase
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
