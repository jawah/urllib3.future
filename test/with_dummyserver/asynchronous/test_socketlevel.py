from __future__ import annotations

import os
import asyncio
import contextlib
import ssl
import typing

import pytest
from jh2.config import H2Configuration  # type: ignore[import-untyped]
from jh2.connection import H2Connection  # type: ignore[import-untyped]
from jh2.events import RequestReceived  # type: ignore[import-untyped]
from urllib3 import HttpVersion, ResponsePromise, AsyncProxyManager
from urllib3 import AsyncHTTPConnectionPool, AsyncHTTPSConnectionPool, AsyncPoolManager
from urllib3._async.connection import AsyncHTTPConnection
from urllib3.contrib.ssa import AsyncSocket
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
    SSLError,
)

from dummyserver.server import DEFAULT_CA, DEFAULT_CERTS
from dummyserver.testcase import SocketDummyServerTestCase, consume_socket
from test.conftest import ServerConfig
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
    async def test_close_with_completed_read(self) -> None:
        body = b"data: ready\n\n"
        self.start_response_handler(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        async with AsyncPoolManager(timeout=5) as manager:
            response = await manager.request("GET", f"psse://{self.host}:{self.port}/")
            extension = response.extension
            assert isinstance(extension, AsyncServerSideEventExtensionFromHTTP)
            assert extension._stream is not None
            # Hold the real read at completion, before next_payload() could resume
            # and clear its task reference. This makes the close race deterministic.
            read = asyncio.create_task(extension._stream.__anext__())
            extension._next_value_task = read
            done, _ = await asyncio.wait([read], timeout=5)
            assert read in done
            await asyncio.wait_for(extension.close(), 5)
            assert not read.cancelled()
            assert read.result() == body
            assert extension.closed
            await extension.close()
            await response.close()

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
    async def test_close_before_response_headers_releases_socket(self) -> None:
        from urllib3.exceptions import HTTPError

        request_received, peer_closed = Event(), Event()

        def handler(listener: socket.socket) -> None:
            listener.settimeout(5)
            with listener.accept()[0] as sock:
                sock.settimeout(5)
                headers = bytearray()
                while not headers.endswith(b"\r\n\r\n"):
                    chunk = sock.recv(65536)
                    assert chunk
                    headers.extend(chunk)
                request_received.set()
                assert sock.recv(1) == b""
                peer_closed.set()

        self._start_server(handler)
        loop = asyncio.get_running_loop()
        async with AsyncPoolManager(timeout=5, retries=False) as manager:
            request = asyncio.create_task(
                manager.request("GET", f"http://{self.host}:{self.port}/")
            )
            try:
                assert await loop.run_in_executor(None, request_received.wait, 5)
                await manager.clear()
                with pytest.raises((HTTPError, asyncio.CancelledError)):
                    await asyncio.wait_for(request, 5)
                assert await loop.run_in_executor(None, peer_closed.wait, 5)
                assert manager.pools.rsize() == 0
            finally:
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)

    async def test_manager_clear_reaches_busy_child_pool(self) -> None:
        finish_body, first_closed, second_closed = Event(), Event(), Event()

        def read_request(sock: socket.socket) -> None:
            sock.settimeout(5)
            headers = bytearray()
            while not headers.endswith(b"\r\n\r\n"):
                chunk = sock.recv(65536)
                assert chunk
                headers.extend(chunk)

        def handler(listener: socket.socket) -> None:
            listener.settimeout(5)
            with listener.accept()[0] as first:
                read_request(first)
                first.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\naa")
                with listener.accept()[0] as second:
                    read_request(second)
                    second.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nb")
                    try:
                        assert first.recv(1) == b""
                    except ConnectionResetError:
                        pass
                    first_closed.set()
                    assert finish_body.wait(5)
                    try:
                        second.sendall(b"b")
                        assert second.recv(1) == b""
                    except (ConnectionResetError, BrokenPipeError):
                        pass
                    second_closed.set()

        self._start_server(handler)
        connections: list[AsyncHTTPConnection] = []
        loop = asyncio.get_running_loop()
        try:
            async with AsyncPoolManager(timeout=5, retries=False) as manager:
                url = f"http://{self.host}:{self.port}/"
                first = await manager.request("GET", url, preload_content=False)
                assert isinstance(first.connection, AsyncHTTPConnection)
                connections.append(first.connection)
                second = await manager.request("GET", url, preload_content=False)
                assert isinstance(second.connection, AsyncHTTPConnection)
                connections.append(second.connection)
                assert connections[0] is not connections[1]
                first_sock, second_sock = [conn.sock for conn in connections]
                assert first_sock is not None and second_sock is not None
                police = second._police_officer
                assert police is not None
                assert await first.read() == b"aa"
                assert first_sock.fileno() >= 0
                await manager.clear()
                assert police._shutdown
                assert first_sock.fileno() == -1
                assert await loop.run_in_executor(None, first_closed.wait, 5)
                assert second_sock.fileno() >= 0
                finish_body.set()
                assert await second.read() == b"bb"
                assert second_sock.fileno() == -1
                assert await loop.run_in_executor(None, second_closed.wait, 5)
                assert not police._shutdown
        finally:
            finish_body.set()
            for conn in connections:
                await conn.close()

    async def test_manager_clear_during_request_cleans_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from urllib3 import AsyncHTTPResponse

        reading_body, finish_body, peer_closed = Event(), Event(), Event()
        raw_read = AsyncHTTPResponse._raw_read

        async def read_body(
            response: AsyncHTTPResponse, *args: typing.Any, **kwargs: typing.Any
        ) -> bytes:
            # Signal once the response exists and the client starts reading its body.
            reading_body.set()
            return await raw_read(response, *args, **kwargs)

        monkeypatch.setattr(AsyncHTTPResponse, "_raw_read", read_body)

        def handler(listener: socket.socket) -> None:
            listener.settimeout(5)
            with listener.accept()[0] as sock:
                sock.settimeout(5)
                headers = bytearray()
                while not headers.endswith(b"\r\n\r\n"):
                    chunk = sock.recv(65536)
                    assert chunk
                    headers.extend(chunk)
                sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\no")
                assert finish_body.wait(5)
                try:
                    sock.sendall(b"k")
                    assert sock.recv(1) == b""
                except (BrokenPipeError, ConnectionResetError):
                    pass
                peer_closed.set()

        self._start_server(handler)
        loop = asyncio.get_running_loop()
        async with AsyncPoolManager(timeout=5, retries=False) as manager:
            future = asyncio.create_task(
                manager.request("GET", f"http://{self.host}:{self.port}/")
            )
            try:
                assert await loop.run_in_executor(None, reading_body.wait, 5)
                police = next(iter(manager.pools._registry.values())).pool
                assert police is not None
                await manager.clear()
                finish_body.set()
                response = await asyncio.wait_for(future, 5)
                assert await response.data == b"ok"
                assert await loop.run_in_executor(None, peer_closed.wait, 5)
                assert police.rsize() == 0
                assert manager.pools.rsize() == 0
            finally:
                finish_body.set()
                if not future.done():
                    future.cancel()
                await asyncio.gather(future, return_exceptions=True)

    @pytest.mark.parametrize(
        "owner_kind", ["pool", "manager", "direct-child", "adapter"]
    )
    @pytest.mark.parametrize("close_owner", [False, True])
    @pytest.mark.parametrize("stream", [False, True])
    async def test_streamed_response_outlives_owner(
        self, owner_kind: str, close_owner: bool, stream: bool
    ) -> None:
        finish_body, peer_closed = Event(), Event()
        body = b"first" + b"x" * 1024

        def handler(listener: socket.socket) -> None:
            listener.settimeout(5)
            with listener.accept()[0] as sock:
                sock.settimeout(5)
                for index in range(1 if close_owner else 2):
                    headers = bytearray()
                    while not headers.endswith(b"\r\n\r\n"):
                        chunk = sock.recv(65536)
                        if not chunk:
                            return
                        headers.extend(chunk)
                    sock.sendall(
                        f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode()
                        + body[:5]
                    )
                    if index == 0:
                        assert finish_body.wait(5)
                    sock.sendall(body[5:])
                assert sock.recv(1) == b""
                peer_closed.set()

        self._start_server(handler)
        owner = (
            AsyncPoolManager(timeout=5, retries=False)
            if owner_kind != "pool"
            else AsyncHTTPConnectionPool(self.host, self.port, timeout=5, retries=False)
        )
        url = f"http://{self.host}:{self.port}/" if owner_kind != "pool" else "/"
        conn = None
        try:
            async with contextlib.AsyncExitStack() as stack:
                await stack.enter_async_context(owner)
                requester = owner
                if owner_kind in ("direct-child", "adapter"):
                    assert isinstance(owner, AsyncPoolManager)
                    # Requests gets a child pool directly instead of calling manager.urlopen().
                    requester = await owner.connection_from_url(url)
                    url = "/"
                response = await requester.request("GET", url, preload_content=False)
                if owner_kind == "adapter":
                    assert isinstance(owner, AsyncPoolManager)
                    assert isinstance(requester, AsyncHTTPConnectionPool)
                    # Niquests returns the parent checkout before reading the body.
                    owner.pools.memorize(response, requester)
                    owner.pools.release()
                    assert not owner.pools.busy
                candidate = response.connection
                assert isinstance(candidate, AsyncHTTPConnection)
                conn = candidate
                assert conn.sock is not None
                http_pool = response._pool
                assert http_pool is not None
                watcher = http_pool._background_monitoring
                assert watcher is not None
                # Keep strong references: GC must not close this socket.
                sock = conn.sock
                if close_owner:
                    await stack.aclose()
                    assert http_pool.pool is None
                    assert watcher.done()
                assert sock.fileno() >= 0
                assert await response.read(1) == body[:1]
                assert sock.fileno() >= 0
                finish_body.set()
                remaining = (
                    b"".join([chunk async for chunk in response.stream(17)])
                    if stream
                    else await response.read()
                )
                assert remaining == body[1:]
                if isinstance(owner, AsyncPoolManager):
                    assert id(response) not in owner.pools._map
                if close_owner:
                    assert sock.fileno() == -1
                    if isinstance(owner, AsyncPoolManager):
                        assert owner.pools.rsize() == 0
                else:
                    assert conn.sock is sock and sock.fileno() >= 0
                await response.close()
                if not close_owner:
                    if owner_kind == "adapter":
                        assert isinstance(owner, AsyncPoolManager)
                        requester = await owner.connection_from_url(
                            f"http://{self.host}:{self.port}/"
                        )
                    followup = await requester.request(
                        "GET", url, preload_content=False
                    )
                    if owner_kind == "adapter":
                        assert isinstance(owner, AsyncPoolManager)
                        assert isinstance(requester, AsyncHTTPConnectionPool)
                        owner.pools.memorize(followup, requester)
                        owner.pools.release()
                    assert await followup.data == body
                    assert conn.sock is sock
            assert await asyncio.get_running_loop().run_in_executor(
                None, peer_closed.wait, 5
            ), "Client did not close the retired connection"
        finally:
            finish_body.set()
            if conn is not None:
                await conn.close()

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
                        except (ssl.SSLError, ConnectionResetError, BlockingIOError):
                            # Older LibreSSL can report EAGAIN after the peer closes.
                            pass
                        except OSError as exc:
                            # OpenSSL 1.0.2 can report the client's TCP close as errno 0.
                            if exc.errno != 0:
                                raise

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

    @pytest.mark.parametrize("peer_event", ["goaway", "invalid-ping"])
    async def test_idle_http2_event_before_reuse(self, peer_event: str) -> None:
        send_event = Event()
        event_sent = Event()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(DEFAULT_CERTS["certfile"], DEFAULT_CERTS["keyfile"])
        context.set_alpn_protocols(["h2"])

        def handler(listener: socket.socket) -> None:
            listener.settimeout(5)
            for index in range(2):
                with listener.accept()[0] as raw:
                    raw.settimeout(5)
                    with context.wrap_socket(raw, server_side=True) as sock:
                        h2 = H2Connection(config=H2Configuration(client_side=False))
                        h2.initiate_connection()
                        sock.sendall(h2.data_to_send())
                        responded = False
                        while not responded:
                            data = sock.recv(65536)
                            assert data
                            for event in h2.receive_data(data):
                                if isinstance(event, RequestReceived):
                                    h2.send_headers(
                                        event.stream_id,
                                        [
                                            (b":status", b"200"),
                                            (b"content-length", b"2"),
                                        ],
                                    )
                                    h2.send_data(
                                        event.stream_id, b"ok", end_stream=True
                                    )
                                    responded = True
                            sock.sendall(h2.data_to_send())
                        if index == 0:
                            assert send_event.wait(5)
                            if peer_event == "goaway":
                                h2.close_connection(last_stream_id=1)
                                sock.sendall(h2.data_to_send())
                            else:
                                # PING requires eight payload bytes; send seven.
                                sock.sendall(
                                    b"\x00\x00\x07\x06\x00\x00\x00\x00\x00invalid"
                                )
                            event_sent.set()
                        # Keep TCP open: the client must act on the HTTP/2 event.
                        try:
                            while sock.recv(65536):
                                pass
                        except ConnectionResetError:
                            pass
                        except ssl.SSLError as error:
                            # Older Python can expose ragged EOF as a generic SSLError.
                            if (
                                not isinstance(error, ssl.SSLEOFError)
                                and "unexpected eof" not in str(error).lower()
                            ):
                                raise

        self._start_server(handler)
        async with AsyncPoolManager(
            ssl_backend="ssl",
            ca_certs=DEFAULT_CA,
            timeout=5,
            disabled_svn={HttpVersion.h3},
            background_watch_delay=None,
            retries=False,
        ) as manager:
            url = f"https://{self.host}:{self.port}/"
            first = await manager.urlopen("GET", url, preload_content=False)
            conn = first.connection
            assert first.version == 20 and conn is not None
            previous = typing.cast(AsyncSocket, conn.sock)
            assert previous is not None
            assert await first.read() == b"ok"
            send_event.set()
            assert await asyncio.get_running_loop().run_in_executor(
                None, event_sent.wait, 5
            )
            await previous.until_data_available(5)
            second = await manager.urlopen("GET", url, preload_content=False)
            assert second.version == 20 and second.connection is not None
            assert previous.fileno() == -1
            current = typing.cast(AsyncSocket, second.connection.sock)
            assert current is not previous
            assert await second.read() == b"ok"

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
        "error_code,error_name",
        [
            (0xB, "ENHANCE_YOUR_CALM"),
            (0x1, "PROTOCOL_ERROR"),
            (0xFFFF, "UNKNOWN_ERROR"),
        ],
    )
    @pytest.mark.parametrize("multiplexed", [False, True])
    async def test_http2_goaway_error_message(
        self, error_code: int, error_name: str, multiplexed: bool
    ) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(DEFAULT_CERTS["certfile"], DEFAULT_CERTS["keyfile"])
        context.set_alpn_protocols(["h2"])

        def handler(listener: socket.socket) -> None:
            listener.settimeout(5)
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
                                h2.close_connection(
                                    error_code=error_code,
                                    last_stream_id=event.stream_id,
                                )
                                received = True
                        sock.sendall(h2.data_to_send())
                    # Keep TCP open until the client processes the actual GOAWAY.
                    try:
                        while sock.recv(65536):
                            pass
                    except (ssl.SSLError, ConnectionResetError):
                        pass

        self._start_server(handler)
        async with AsyncHTTPSConnectionPool(
            self.host,
            self.port,
            ca_certs=DEFAULT_CA,
            timeout=5,
            disabled_svn={HttpVersion.h11, HttpVersion.h3},
            retries=False,
        ) as pool:
            with pytest.raises(ProtocolError) as caught:
                if multiplexed:
                    promise = await pool.urlopen("GET", "/", multiplexed=True)
                    assert isinstance(promise, ResponsePromise)
                    await pool.get_response(promise=promise)
                else:
                    await pool.urlopen("GET", "/")
            message = str(caught.value)
            assert "HTTP/2" in message
            assert "GOAWAY" in message
            assert error_name in message
            assert hex(error_code) in message

    @pytest.mark.parametrize(
        "error_code,frame_type,reason,error_name,error_type",
        [
            (0x107, None, "too many requests", "H3_EXCESSIVE_LOAD", ProtocolError),
            (
                0x200,
                None,
                "invalid QPACK data",
                "QPACK_DECOMPRESSION_FAILED",
                ProtocolError,
            ),
            # The same number belongs to different namespaces on the wire.
            (0x10A, None, "", "H3_MISSING_SETTINGS", ProtocolError),
            (
                0x10A,
                0x6,
                "unexpected handshake message",
                "TLS_UNEXPECTED_MESSAGE",
                SSLError,
            ),
            (0x12A, None, "custom application error", "UNKNOWN_ERROR", ProtocolError),
            (
                0x12A,
                0x6,
                "peer rejected client certificate",
                "TLS_BAD_CERTIFICATE",
                SSLError,
            ),
            (0x128, 0x6, "no compatible cipher", "TLS_HANDSHAKE_FAILURE", SSLError),
            (0x3, 0x8, "flow control exceeded", "FLOW_CONTROL_ERROR", ProtocolError),
            (0xFFFF, None, "custom application error", "UNKNOWN_ERROR", ProtocolError),
            (0x1FF, 0x6, "", "UNKNOWN_ERROR", SSLError),
            # TLS classification must precede the legacy code-400/header heuristic.
            (0x190, 0x6, "invalid header", "UNKNOWN_ERROR", SSLError),
        ],
    )
    @pytest.mark.parametrize("multiplexed", [False, True])
    async def test_http3_close_error_message(
        self,
        http3_error_server: ServerConfig,
        error_code: int,
        frame_type: int | None,
        reason: str,
        error_name: str,
        error_type: type[Exception],
        multiplexed: bool,
    ) -> None:
        async with AsyncHTTPSConnectionPool(
            http3_error_server.host,
            http3_error_server.port,
            ca_certs=http3_error_server.ca_certs,
            timeout=5,
            disabled_svn={HttpVersion.h11, HttpVersion.h2},
            retries=False,
        ) as pool:
            with pytest.raises(error_type) as caught:
                if multiplexed:
                    promise = await pool.urlopen("GET", "/", multiplexed=True)
                    assert isinstance(promise, ResponsePromise)
                    await pool.get_response(promise=promise)
                else:
                    await pool.urlopen("GET", "/")
            message = str(caught.value)
            assert "HTTP/3" in message
            assert error_name in message
            assert hex(error_code) in message
            if reason:
                assert reason in message

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

    @pytest.mark.parametrize("through_manager", [False, True])
    @pytest.mark.parametrize("by_promise", [False, True])
    async def test_retired_pool_connection_error_releases_registry(
        self, through_manager: bool, by_promise: bool
    ) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(DEFAULT_CERTS["certfile"], DEFAULT_CERTS["keyfile"])
        context.set_alpn_protocols(["h2"])
        terminate = Event()

        def handler(listener: socket.socket) -> None:
            listener.settimeout(5)
            with listener.accept()[0] as raw:
                raw.settimeout(5)
                with context.wrap_socket(raw, server_side=True) as sock:
                    h2 = H2Connection(config=H2Configuration(client_side=False))
                    h2.initiate_connection()
                    sock.sendall(h2.data_to_send())
                    while True:
                        data = sock.recv(65536)
                        assert data
                        for event in h2.receive_data(data):
                            if isinstance(event, RequestReceived):
                                assert terminate.wait(5)
                                h2.close_connection(
                                    error_code=0x2, last_stream_id=event.stream_id
                                )
                                sock.sendall(h2.data_to_send())
                                return
                        sock.sendall(h2.data_to_send())

        self._start_server(handler)
        try:
            async with AsyncPoolManager(
                ca_certs=DEFAULT_CA,
                disabled_svn={HttpVersion.h11, HttpVersion.h3},
                timeout=5,
                retries=False,
            ) as manager:
                promise = await manager.urlopen(
                    "GET", f"https://{self.host}:{self.port}/", multiplexed=True
                )
                assert isinstance(promise, ResponsePromise)
                old_pool = next(iter(manager.pools._registry.values()))
                police = old_pool.pool
                assert police is not None
                sock = promise._conn.sock
                assert sock is not None
                await manager.clear()
                terminate.set()
                reader = manager if through_manager else old_pool
                with pytest.raises(ProtocolError, match="GOAWAY"):
                    await reader.get_response(promise=promise if by_promise else None)
                assert sock.fileno() == -1
                assert police.rsize() == 0
                assert manager.pools.rsize() == 0
        finally:
            terminate.set()

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

    async def test_remote_close_cause_does_not_retry_post(self) -> None:
        body = b"already processed"
        received = []

        def socket_handler(listener: socket.socket) -> None:
            listener.settimeout(5)
            with listener.accept()[0] as sock:
                sock.settimeout(5)
                with sock.makefile("rb") as request:
                    assert request.readline().startswith(b"POST / ")
                    while request.readline() not in (b"\r\n", b""):
                        pass
                    received.append(request.read(len(body)))
                # The request was delivered; close without acknowledging it.

        self._start_server(socket_handler)
        async with AsyncHTTPConnectionPool(
            self.host, self.port, timeout=5, retries=1
        ) as pool:
            with pytest.raises(
                ProtocolError, match="Remote end closed connection without response"
            ) as caught:
                await pool.request("POST", "/", body=body)

            assert isinstance(caught.value.__cause__, ConnectionResetError)
            assert str(caught.value.__cause__) == str(caught.value)
            assert pool.num_requests == pool.num_connections == 1
        assert received == [body]


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
    @pytest.mark.parametrize("compressed", [False, True])
    @pytest.mark.parametrize("decode_content", [False, True])
    async def test_read1_size_preserves_remainder(
        self, compressed: bool, decode_content: bool
    ) -> None:
        import zlib

        payload = b"response body" * 100
        body = zlib.compress(payload) if compressed else payload
        encoding = b"Content-Encoding: deflate\r\n" if compressed else b""
        self.start_response_handler(
            b"HTTP/1.1 200 OK\r\n"
            + encoding
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        async with AsyncHTTPConnectionPool(self.host, self.port, timeout=5) as pool:
            response = await pool.urlopen("GET", "/", preload_content=False)
            try:
                received = b""
                for size in (1, 7, 2, 16):
                    chunk = await response.read1(size, decode_content=decode_content)
                    assert len(chunk) <= size
                    received += chunk
                received += await response.read(decode_content=decode_content)
                assert received == (payload if decode_content else body)
            finally:
                await response.close()

    @pytest.mark.parametrize("incomplete_utf8", [False, True])
    async def test_sse_utf8_at_eof(self, incomplete_utf8: bool) -> None:
        body = "data: first €\n\n".encode()
        if incomplete_utf8:
            body += b"data: incomplete \xe2\x82"
        self.start_response_handler(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        async with AsyncPoolManager(timeout=5) as manager:
            response = await manager.urlopen("GET", f"psse://{self.host}:{self.port}/")
            assert response.extension is not None
            event = await response.extension.next_payload()
            assert isinstance(event, ServerSentEvent)
            assert event.data == "first €"
            if incomplete_utf8:
                with pytest.raises(UnicodeDecodeError):
                    await response.extension.next_payload()
            else:
                assert await response.extension.next_payload() is None
                assert response.extension.closed
        assert manager.pools.rsize() == 0

    @pytest.mark.parametrize("partial", [False, True])
    @pytest.mark.parametrize("chunked", [False, True])
    async def test_compressed_response_read_boundaries(
        self,
        compressed_payload: tuple[str, bytes, bytes],
        partial: bool,
        chunked: bool,
    ) -> None:
        encoding, encoded, plain = compressed_payload
        headers = f"HTTP/1.1 200 OK\r\nContent-Encoding: {encoding}\r\nConnection: close\r\n".encode()
        if chunked:
            wire = (
                headers
                + b"Transfer-Encoding: chunked\r\n\r\n"
                + f"{len(encoded):x}\r\n".encode()
                + encoded
                + b"\r\n0\r\n\r\n"
            )
        else:
            wire = (
                headers + f"Content-Length: {len(encoded)}\r\n\r\n".encode() + encoded
            )

        def handler(listener: socket.socket) -> None:
            with listener.accept()[0] as sock:
                sock.settimeout(5)
                consume_socket(sock)
                sock.sendall(wire)

        self._start_server(handler)
        async with AsyncHTTPConnectionPool(
            self.host, self.port, timeout=5, retries=False
        ) as pool:
            response = await pool.urlopen("GET", "/", preload_content=False)
            from urllib3.backend._async._base import AsyncLowLevelResponse

            assert isinstance(response._fp, AsyncLowLevelResponse)
            assert response._fp.from_promise is not None
            assert response.is_from_promise(response._fp.from_promise)
            prefix = await response.read(64) if partial else b""
            assert prefix == (plain[:64] if partial else b"")
            assert await response.read(0) == b""
            assert await response.read1(0) == b""
            assert [part async for part in response.stream(0)] == []
            if chunked:
                remainder = b"".join([part async for part in response.read_chunked(-1)])
            else:
                remainder = await response.read()
            assert len(prefix + remainder) == len(plain)
            assert prefix + remainder == plain
            await response.close()

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

    @pytest.mark.parametrize("decode_content", [False, True])
    async def test_zero_sized_read_does_not_wait_for_body(
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
                assert await response.read(0, decode_content=decode_content) == b""
                assert response.tell() == 0
                assert response.length_remaining == 4
                send_body.set()
                assert await response.read(cache_content=True) == b"body"
                assert await response.data == b"body"
            finally:
                send_body.set()
                await response.close()

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
