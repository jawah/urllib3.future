from __future__ import annotations

import socket
import threading
import typing
from test import SHORT_TIMEOUT

import pytest

from dummyserver.server import DEFAULT_CA, DEFAULT_CERTS
from dummyserver.testcase import IPV4SocketDummyServerTestCase
from urllib3.contrib import socks
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError

try:
    import ssl

    from urllib3.util import ssl_ as better_ssl

    HAS_SSL = True
except ImportError:
    ssl = None  # type: ignore[assignment]
    better_ssl = None  # type: ignore[assignment]
    HAS_SSL = False


SOCKS_NEGOTIATION_NONE = b"\x00"
SOCKS_NEGOTIATION_PASSWORD = b"\x02"

SOCKS_VERSION_SOCKS4 = b"\x04"
SOCKS_VERSION_SOCKS5 = b"\x05"


def _get_free_port(host: str) -> int:
    """
    Gets a free port by opening a socket, binding it, checking the assigned
    port, and then closing it.
    """
    s = socket.socket()
    s.bind((host, 0))
    port = s.getsockname()[1]
    s.close()
    return port  # type: ignore[no-any-return]


def _read_exactly(sock: socket.socket, amt: int) -> bytes:
    """
    Read *exactly* ``amt`` bytes from the socket ``sock``.
    """
    data = b""

    while amt > 0:
        chunk = sock.recv(amt)
        data += chunk
        amt -= len(chunk)

    return data


def _read_until(sock: socket.socket, char: bytes) -> bytes:
    """
    Read from the socket until the character is received.
    """
    chunks = []
    while True:
        chunk = sock.recv(1)
        chunks.append(chunk)
        if chunk == char:
            break

    return b"".join(chunks)


def _address_from_socket(sock: socket.socket) -> bytes | str:
    """
    Returns the address from the SOCKS socket
    """
    addr_type = sock.recv(1)

    if addr_type == b"\x01":
        ipv4_addr = _read_exactly(sock, 4)
        return socket.inet_ntoa(ipv4_addr)
    elif addr_type == b"\x04":
        ipv6_addr = _read_exactly(sock, 16)
        return socket.inet_ntop(socket.AF_INET6, ipv6_addr)
    elif addr_type == b"\x03":
        addr_len = ord(sock.recv(1))
        return _read_exactly(sock, addr_len)
    else:
        raise RuntimeError(f"Unexpected addr type: {addr_type!r}")


def handle_socks5_negotiation(
    sock: socket.socket,
    negotiate: bool,
    username: bytes | None = None,
    password: bytes | None = None,
) -> typing.Generator[tuple[bytes | str, int], bool, None]:
    """
    Handle the SOCKS5 handshake.

    Returns a generator object that allows us to break the handshake into
    steps so that the test code can intervene at certain useful points.
    """
    received_version = sock.recv(1)
    assert received_version == SOCKS_VERSION_SOCKS5
    nmethods = ord(sock.recv(1))
    methods = _read_exactly(sock, nmethods)

    if negotiate:
        assert SOCKS_NEGOTIATION_PASSWORD in methods
        send_data = SOCKS_VERSION_SOCKS5 + SOCKS_NEGOTIATION_PASSWORD
        sock.sendall(send_data)

        # This is the password negotiation.
        negotiation_version = sock.recv(1)
        assert negotiation_version == b"\x01"
        ulen = ord(sock.recv(1))
        provided_username = _read_exactly(sock, ulen)
        plen = ord(sock.recv(1))
        provided_password = _read_exactly(sock, plen)

        if username == provided_username and password == provided_password:
            sock.sendall(b"\x01\x00")
        else:
            sock.sendall(b"\x01\x01")
            sock.close()
            return
    else:
        assert SOCKS_NEGOTIATION_NONE in methods
        send_data = SOCKS_VERSION_SOCKS5 + SOCKS_NEGOTIATION_NONE
        sock.sendall(send_data)

    # Client sends where they want to go.
    received_version = sock.recv(1)
    command = sock.recv(1)
    reserved = sock.recv(1)
    addr = _address_from_socket(sock)
    port_raw = _read_exactly(sock, 2)
    port = (ord(port_raw[0:1]) << 8) + (ord(port_raw[1:2]))

    # Check some basic stuff.
    assert received_version == SOCKS_VERSION_SOCKS5
    assert command == b"\x01"  # Only support connect, not bind.
    assert reserved == b"\x00"

    # Yield the address port tuple.
    succeed = yield addr, port

    if succeed:
        # Hard-coded response for now.
        response = SOCKS_VERSION_SOCKS5 + b"\x00\x00\x01\x7f\x00\x00\x01\xea\x60"
    else:
        # Hard-coded response for now.
        response = SOCKS_VERSION_SOCKS5 + b"\x01\00"

    sock.sendall(response)


def handle_socks4_negotiation(
    sock: socket.socket, username: bytes | None = None
) -> typing.Generator[tuple[bytes | str, int], bool, None]:
    """
    Handle the SOCKS4 handshake.

    Returns a generator object that allows us to break the handshake into
    steps so that the test code can intervene at certain useful points.
    """
    received_version = sock.recv(1)
    command = sock.recv(1)
    port_raw = _read_exactly(sock, 2)
    port = (ord(port_raw[0:1]) << 8) + (ord(port_raw[1:2]))
    addr_raw = _read_exactly(sock, 4)
    provided_username = _read_until(sock, b"\x00")[:-1]  # Strip trailing null.

    addr: bytes | str
    if addr_raw == b"\x00\x00\x00\x01":
        # Magic string: means DNS name.
        addr = _read_until(sock, b"\x00")[:-1]  # Strip trailing null.
    else:
        addr = socket.inet_ntoa(addr_raw)

    # Check some basic stuff.
    assert received_version == SOCKS_VERSION_SOCKS4
    assert command == b"\x01"  # Only support connect, not bind.

    if username is not None and username != provided_username:
        sock.sendall(b"\x00\x5d\x00\x00\x00\x00\x00\x00")
        sock.close()
        return

    # Yield the address port tuple.
    succeed = yield addr, port

    if succeed:
        response = b"\x00\x5a\xea\x60\x7f\x00\x00\x01"
    else:
        response = b"\x00\x5b\x00\x00\x00\x00\x00\x00"

    sock.sendall(response)


@pytest.mark.asyncio
class TestSocks5Proxy(IPV4SocketDummyServerTestCase):
    """
    Test the SOCKS proxy in SOCKS5 mode.
    """

    async def test_basic_request(self) -> None:
        def request_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            handler = handle_socks5_negotiation(sock, negotiate=False)
            addr, port = next(handler)

            assert addr == "16.17.18.19"
            assert port == 80
            with pytest.raises(StopIteration):
                handler.send(True)

            while True:
                buf = sock.recv(65535)
                if buf.endswith(b"\r\n\r\n"):
                    break

            sock.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Server: SocksTestServer\r\n"
                b"Content-Length: 0\r\n"
                b"\r\n"
            )
            sock.close()

        self._start_server(request_handler)
        proxy_url = f"socks5://{self.host}:{self.port}"
        async with socks.AsyncSOCKSProxyManager(proxy_url) as pm:
            response = await pm.request("GET", "http://16.17.18.19")

            assert response.status == 200
            assert await response.data == b""
            assert response.headers["Server"] == "SocksTestServer"

    async def test_local_dns(self) -> None:
        def request_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            handler = handle_socks5_negotiation(sock, negotiate=False)
            addr, port = next(handler)

            assert addr in ["127.0.0.1", "::1"]
            assert port == 80
            with pytest.raises(StopIteration):
                handler.send(True)

            while True:
                buf = sock.recv(65535)
                if buf.endswith(b"\r\n\r\n"):
                    break

            sock.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Server: SocksTestServer\r\n"
                b"Content-Length: 0\r\n"
                b"\r\n"
            )
            sock.close()

        self._start_server(request_handler)
        proxy_url = f"socks5://{self.host}:{self.port}"
        async with socks.AsyncSOCKSProxyManager(proxy_url) as pm:
            response = await pm.request("GET", "http://localhost")

            assert response.status == 200
            assert await response.data == b""
            assert response.headers["Server"] == "SocksTestServer"

    async def test_correct_header_line(self) -> None:
        def request_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            handler = handle_socks5_negotiation(sock, negotiate=False)
            addr, port = next(handler)

            assert addr == b"example.com"
            assert port == 80
            with pytest.raises(StopIteration):
                handler.send(True)

            buf = b""
            while True:
                buf += sock.recv(65535)
                if buf.endswith(b"\r\n\r\n"):
                    break

            assert buf.startswith(b"GET / HTTP/1.1")
            assert b"Host: example.com" in buf

            sock.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Server: SocksTestServer\r\n"
                b"Content-Length: 0\r\n"
                b"\r\n"
            )
            sock.close()

        self._start_server(request_handler)
        proxy_url = f"socks5h://{self.host}:{self.port}"
        async with socks.AsyncSOCKSProxyManager(proxy_url) as pm:
            response = await pm.request("GET", "http://example.com")
            assert response.status == 200

    async def test_connection_timeouts(self) -> None:
        event = threading.Event()

        def request_handler(listener: socket.socket) -> None:
            event.wait()

        self._start_server(request_handler)
        proxy_url = f"socks5h://{self.host}:{self.port}"
        async with socks.AsyncSOCKSProxyManager(proxy_url) as pm:
            with pytest.raises(ConnectTimeoutError):
                await pm.request(
                    "GET", "http://example.com", timeout=SHORT_TIMEOUT, retries=False
                )
            event.set()

    async def test_connection_failure(self) -> None:
        event = threading.Event()

        def request_handler(listener: socket.socket) -> None:
            listener.close()
            event.set()

        self._start_server(request_handler)
        proxy_url = f"socks5h://{self.host}:{self.port}"
        async with socks.AsyncSOCKSProxyManager(proxy_url) as pm:
            event.wait()
            with pytest.raises(NewConnectionError):
                await pm.request("GET", "http://example.com", retries=False)

    async def test_socks_with_password(self) -> None:
        def request_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            handler = handle_socks5_negotiation(
                sock, negotiate=True, username=b"user", password=b"pass"
            )
            addr, port = next(handler)

            assert addr == "16.17.18.19"
            assert port == 80
            with pytest.raises(StopIteration):
                handler.send(True)

            while True:
                buf = sock.recv(65535)
                if buf.endswith(b"\r\n\r\n"):
                    break

            sock.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Server: SocksTestServer\r\n"
                b"Content-Length: 0\r\n"
                b"\r\n"
            )
            sock.close()

        self._start_server(request_handler)
        proxy_url = f"socks5://{self.host}:{self.port}"
        async with socks.AsyncSOCKSProxyManager(
            proxy_url, username="user", password="pass"
        ) as pm:
            response = await pm.request("GET", "http://16.17.18.19")

            assert response.status == 200
            assert await response.data == b""
            assert response.headers["Server"] == "SocksTestServer"

    async def test_socks_with_auth_in_url(self) -> None:
        """
        Test when we have auth info in url, i.e.
        socks5://user:pass@host:port and no username/password as params
        """

        def request_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            handler = handle_socks5_negotiation(
                sock, negotiate=True, username=b"user", password=b"pass"
            )
            addr, port = next(handler)

            assert addr == "16.17.18.19"
            assert port == 80
            with pytest.raises(StopIteration):
                handler.send(True)

            while True:
                buf = sock.recv(65535)
                if buf.endswith(b"\r\n\r\n"):
                    break

            sock.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Server: SocksTestServer\r\n"
                b"Content-Length: 0\r\n"
                b"\r\n"
            )
            sock.close()

        self._start_server(request_handler)
        proxy_url = f"socks5://user:pass@{self.host}:{self.port}"
        async with socks.AsyncSOCKSProxyManager(proxy_url) as pm:
            response = await pm.request("GET", "http://16.17.18.19")

            assert response.status == 200
            assert await response.data == b""
            assert response.headers["Server"] == "SocksTestServer"

    async def test_source_address_works(self) -> None:
        expected_port = _get_free_port(self.host)

        def request_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            assert sock.getpeername()[0] == "127.0.0.1"
            assert sock.getpeername()[1] == expected_port

            handler = handle_socks5_negotiation(sock, negotiate=False)
            addr, port = next(handler)

            assert addr == "16.17.18.19"
            assert port == 80
            with pytest.raises(StopIteration):
                handler.send(True)

            while True:
                buf = sock.recv(65535)
                if buf.endswith(b"\r\n\r\n"):
                    break

            sock.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Server: SocksTestServer\r\n"
                b"Content-Length: 0\r\n"
                b"\r\n"
            )
            sock.close()

        self._start_server(request_handler)
        proxy_url = f"socks5://{self.host}:{self.port}"
        async with socks.AsyncSOCKSProxyManager(
            proxy_url, source_address=("127.0.0.1", expected_port)
        ) as pm:
            response = await pm.request("GET", "http://16.17.18.19")
            assert response.status == 200


@pytest.mark.asyncio
class TestSOCKS4Proxy(IPV4SocketDummyServerTestCase):
    """
    Test the SOCKS proxy in SOCKS4 mode.

    Has relatively fewer tests than the SOCKS5 case, mostly because once the
    negotiation is done the two cases behave identically.
    """

    async def test_basic_request(self) -> None:
        def request_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            handler = handle_socks4_negotiation(sock)
            addr, port = next(handler)

            assert addr == "16.17.18.19"
            assert port == 80
            with pytest.raises(StopIteration):
                handler.send(True)

            while True:
                buf = sock.recv(65535)
                if buf.endswith(b"\r\n\r\n"):
                    break

            sock.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Server: SocksTestServer\r\n"
                b"Content-Length: 0\r\n"
                b"\r\n"
            )
            sock.close()

        self._start_server(request_handler)
        proxy_url = f"socks4://{self.host}:{self.port}"
        async with socks.AsyncSOCKSProxyManager(proxy_url) as pm:
            response = await pm.request("GET", "http://16.17.18.19")

            assert response.status == 200
            assert response.headers["Server"] == "SocksTestServer"
            assert await response.data == b""

    async def test_local_dns(self) -> None:
        def request_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            handler = handle_socks4_negotiation(sock)
            addr, port = next(handler)

            assert addr == "127.0.0.1"
            assert port == 80
            with pytest.raises(StopIteration):
                handler.send(True)

            while True:
                buf = sock.recv(65535)
                if buf.endswith(b"\r\n\r\n"):
                    break

            sock.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Server: SocksTestServer\r\n"
                b"Content-Length: 0\r\n"
                b"\r\n"
            )
            sock.close()

        self._start_server(request_handler)
        proxy_url = f"socks4://{self.host}:{self.port}"
        async with socks.AsyncSOCKSProxyManager(proxy_url) as pm:
            response = await pm.request("GET", "http://localhost")

            assert response.status == 200
            assert response.headers["Server"] == "SocksTestServer"
            assert await response.data == b""

    async def test_correct_header_line(self) -> None:
        def request_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            handler = handle_socks4_negotiation(sock)
            addr, port = next(handler)

            assert addr == b"example.com"
            assert port == 80
            with pytest.raises(StopIteration):
                handler.send(True)

            buf = b""
            while True:
                buf += sock.recv(65535)
                if buf.endswith(b"\r\n\r\n"):
                    break

            assert buf.startswith(b"GET / HTTP/1.1")
            assert b"Host: example.com" in buf

            sock.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Server: SocksTestServer\r\n"
                b"Content-Length: 0\r\n"
                b"\r\n"
            )
            sock.close()

        self._start_server(request_handler)
        proxy_url = f"socks4a://{self.host}:{self.port}"
        async with socks.AsyncSOCKSProxyManager(proxy_url) as pm:
            response = await pm.request("GET", "http://example.com")
            assert response.status == 200

    async def test_socks4_with_username(self) -> None:
        def request_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            handler = handle_socks4_negotiation(sock, username=b"user")
            addr, port = next(handler)

            assert addr == "16.17.18.19"
            assert port == 80
            with pytest.raises(StopIteration):
                handler.send(True)

            while True:
                buf = sock.recv(65535)
                if buf.endswith(b"\r\n\r\n"):
                    break

            sock.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Server: SocksTestServer\r\n"
                b"Content-Length: 0\r\n"
                b"\r\n"
            )
            sock.close()

        self._start_server(request_handler)
        proxy_url = f"socks4://{self.host}:{self.port}"
        async with socks.AsyncSOCKSProxyManager(proxy_url, username="user") as pm:
            response = await pm.request("GET", "http://16.17.18.19")

            assert response.status == 200
            assert await response.data == b""
            assert response.headers["Server"] == "SocksTestServer"

    async def test_socks_with_invalid_username(self) -> None:
        def request_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            handler = handle_socks4_negotiation(sock, username=b"user")
            next(handler, None)

        self._start_server(request_handler)
        proxy_url = f"socks4a://{self.host}:{self.port}"
        async with socks.AsyncSOCKSProxyManager(proxy_url, username="baduser") as pm:
            with pytest.raises(NewConnectionError, match="different user-ids"):
                await pm.request("GET", "http://example.com", retries=False)


@pytest.mark.asyncio
class TestSOCKSWithTLS(IPV4SocketDummyServerTestCase):
    """
    Test that TLS behaves properly for SOCKS proxies.
    """

    @pytest.mark.skipif(not HAS_SSL, reason="No TLS available")
    async def test_basic_request(self) -> None:
        def request_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            handler = handle_socks5_negotiation(sock, negotiate=False)
            addr, port = next(handler)

            assert addr == b"localhost"
            assert port == 443
            with pytest.raises(StopIteration):
                handler.send(True)

            # Wrap in TLS
            context = better_ssl.SSLContext(ssl.PROTOCOL_SSLv23)  # type: ignore[misc]
            context.load_cert_chain(DEFAULT_CERTS["certfile"], DEFAULT_CERTS["keyfile"])
            tls = context.wrap_socket(sock, server_side=True)
            buf = b""

            while True:
                buf += tls.recv(65535)
                if buf.endswith(b"\r\n\r\n"):
                    break

            assert buf.startswith(b"GET / HTTP/1.1\r\n")

            tls.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Server: SocksTestServer\r\n"
                b"Content-Length: 0\r\n"
                b"\r\n"
            )
            tls.close()
            sock.close()

        self._start_server(request_handler)
        proxy_url = f"socks5h://{self.host}:{self.port}"
        async with socks.AsyncSOCKSProxyManager(proxy_url, ca_certs=DEFAULT_CA) as pm:
            response = await pm.request("GET", "https://localhost")

            assert response.status == 200
            assert await response.data == b""
            assert response.headers["Server"] == "SocksTestServer"
