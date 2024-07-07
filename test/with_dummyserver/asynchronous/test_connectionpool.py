from __future__ import annotations

import asyncio
import io
import platform
import socket
import sys
import time
import typing
import warnings
from test import LONG_TIMEOUT, SHORT_TIMEOUT
from threading import Event
from unittest import mock
from urllib.parse import urlencode

import pytest

from dummyserver.server import HAS_IPV6_AND_DNS, NoIPv6Warning
from dummyserver.testcase import HTTPDummyServerTestCase, SocketDummyServerTestCase
from urllib3 import AsyncHTTPConnectionPool, ResponsePromise, encode_multipart_formdata
from urllib3._collections import HTTPHeaderDict
from urllib3._typing import _TYPE_FIELD_VALUE_TUPLE, _TYPE_TIMEOUT
from urllib3.connection import _get_default_user_agent
from urllib3.exceptions import (
    ConnectTimeoutError,
    DecodeError,
    EmptyPoolError,
    MaxRetryError,
    NameResolutionError,
    NewConnectionError,
    TimeoutError,
    UnrewindableBodyError,
)
from urllib3.util import SKIP_HEADER, SKIPPABLE_HEADERS
from urllib3.util.retry import RequestHistory, Retry
from urllib3.util.timeout import Timeout

from ... import INVALID_SOURCE_ADDRESSES, TARPIT_HOST, VALID_SOURCE_ADDRESSES
from ...port_helpers import find_unused_port


def wait_for_socket(ready_event: Event) -> None:
    ready_event.wait()
    ready_event.clear()


@pytest.mark.asyncio
class TestAsyncConnectionPoolTimeouts(SocketDummyServerTestCase):
    @pytest.mark.skipif(
        platform.system() == "Windows"
        and sys.version_info < (3, 11)
        and sys.version_info >= (3, 8),
        reason="unstable on py 3.8, 3.9 and 3.10",
    )
    async def test_timeout_float(self) -> None:
        block_event = Event()
        ready_event = self.start_basic_handler(block_send=block_event, num=1)

        async with AsyncHTTPConnectionPool(self.host, self.port, retries=False) as pool:
            wait_for_socket(ready_event)
            with pytest.raises(TimeoutError):
                await pool.request("GET", "/", timeout=SHORT_TIMEOUT)
            block_event.set()  # Release block

    @pytest.mark.skipif(
        platform.system() == "Windows"
        and sys.version_info < (3, 11)
        and sys.version_info >= (3, 8),
        reason="unstable on py 3.8, 3.9 and 3.10",
    )
    async def test_conn_closed(self) -> None:
        block_event = Event()
        self.start_basic_handler(block_send=block_event, num=1)

        async with AsyncHTTPConnectionPool(
            self.host, self.port, timeout=SHORT_TIMEOUT, retries=False
        ) as pool:
            conn = await pool._get_conn()
            await pool._put_conn(conn)
            try:
                with pytest.raises(TimeoutError):
                    await pool.urlopen("GET", "/")
                if not conn.is_closed:
                    with pytest.raises(socket.error):
                        await conn.sock.recv(1024)  # type: ignore[union-attr]
            finally:
                await pool._put_conn(conn)

            block_event.set()

    @pytest.mark.skipif(
        platform.system() == "Windows"
        and sys.version_info < (3, 11)
        and sys.version_info >= (3, 8),
        reason="unstable on py 3.8, 3.9 and 3.10",
    )
    async def test_timeout(self) -> None:
        # Requests should time out when expected
        block_event = Event()
        ready_event = self.start_basic_handler(block_send=block_event, num=3)

        # Pool-global timeout
        short_timeout = Timeout(read=SHORT_TIMEOUT)
        async with AsyncHTTPConnectionPool(
            self.host, self.port, timeout=short_timeout, retries=False
        ) as pool:
            wait_for_socket(ready_event)
            block_event.clear()

            with pytest.raises(TimeoutError):
                await pool.request("GET", "/")
            block_event.set()  # Release request

        # Request-specific timeouts should raise errors
        async with AsyncHTTPConnectionPool(
            self.host, self.port, timeout=short_timeout, retries=False
        ) as pool:
            wait_for_socket(ready_event)
            now = time.time()
            with pytest.raises(TimeoutError):
                await pool.request("GET", "/", timeout=LONG_TIMEOUT)
            delta = time.time() - now

            message = "timeout was pool-level SHORT_TIMEOUT rather than request-level LONG_TIMEOUT"
            assert delta >= LONG_TIMEOUT, message
            block_event.set()  # Release request

            # Timeout passed directly to request should raise a request timeout
            wait_for_socket(ready_event)
            with pytest.raises(TimeoutError):
                await pool.request("GET", "/", timeout=SHORT_TIMEOUT)
            block_event.set()  # Release request

    async def test_connect_timeout(self) -> None:
        url = "/"
        host, port = TARPIT_HOST, 80
        timeout = Timeout(connect=SHORT_TIMEOUT)

        # Pool-global timeout
        async with AsyncHTTPConnectionPool(host, port, timeout=timeout) as pool:
            conn = await pool._get_conn()
            with pytest.raises(ConnectTimeoutError):
                await pool._make_request(conn, "GET", url)
            await pool._put_conn(conn)
            # Retries
            retries = Retry(connect=0)
            with pytest.raises(MaxRetryError):
                await pool.request("GET", url, retries=retries)

        # Request-specific connection timeouts
        big_timeout = Timeout(read=LONG_TIMEOUT, connect=LONG_TIMEOUT)
        async with AsyncHTTPConnectionPool(
            host, port, timeout=big_timeout, retries=False
        ) as pool:
            conn = await pool._get_conn()
            with pytest.raises(ConnectTimeoutError):
                await pool._make_request(conn, "GET", url, timeout=timeout)

            await pool._put_conn(conn)
            with pytest.raises(ConnectTimeoutError):
                await pool.request("GET", url, timeout=timeout)

    async def test_total_applies_connect(self) -> None:
        host, port = TARPIT_HOST, 80

        timeout = Timeout(total=None, connect=SHORT_TIMEOUT)
        async with AsyncHTTPConnectionPool(host, port, timeout=timeout) as pool:
            conn = await pool._get_conn()
            try:
                with pytest.raises(ConnectTimeoutError):
                    await pool._make_request(conn, "GET", "/")
            finally:
                await conn.close()

        timeout = Timeout(connect=3, read=5, total=SHORT_TIMEOUT)
        async with AsyncHTTPConnectionPool(host, port, timeout=timeout) as pool:
            conn = await pool._get_conn()
            try:
                with pytest.raises(ConnectTimeoutError):
                    await pool._make_request(conn, "GET", "/")
            finally:
                await conn.close()

    @pytest.mark.skipif(
        platform.system() == "Windows"
        and sys.version_info < (3, 11)
        and sys.version_info >= (3, 8),
        reason="unstable on py 3.8, 3.9 and 3.10",
    )
    async def test_total_timeout(self) -> None:
        block_event = Event()
        ready_event = self.start_basic_handler(block_send=block_event, num=2)

        wait_for_socket(ready_event)
        # This will get the socket to raise an EAGAIN on the read
        timeout = Timeout(connect=3, read=SHORT_TIMEOUT)
        async with AsyncHTTPConnectionPool(
            self.host, self.port, timeout=timeout, retries=False
        ) as pool:
            with pytest.raises(TimeoutError):
                await pool.request("GET", "/")

            block_event.set()
            wait_for_socket(ready_event)
            block_event.clear()

        # The connect should succeed and this should hit the read timeout
        timeout = Timeout(connect=3, read=5, total=SHORT_TIMEOUT)
        async with AsyncHTTPConnectionPool(
            self.host, self.port, timeout=timeout, retries=False
        ) as pool:
            with pytest.raises(TimeoutError):
                await pool.request("GET", "/")

    async def test_create_connection_timeout(self) -> None:
        self.start_basic_handler(block_send=Event(), num=0)  # needed for self.port

        timeout = Timeout(connect=SHORT_TIMEOUT, total=LONG_TIMEOUT)
        async with AsyncHTTPConnectionPool(
            TARPIT_HOST, self.port, timeout=timeout, retries=False
        ) as pool:
            conn = await pool._new_conn()
            with pytest.raises(ConnectTimeoutError):
                await conn.connect()


@pytest.mark.asyncio
class TestConnectionPool(HTTPDummyServerTestCase):
    async def test_get(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            r = await pool.request("GET", "/specific_method", fields={"method": "GET"})
            assert r.status == 200, await r.data

    async def test_post_url(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            r = await pool.request(
                "POST", "/specific_method", fields={"method": "POST"}
            )
            assert r.status == 200, await r.data

    async def test_urlopen_put(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            r = await pool.urlopen("PUT", "/specific_method?method=PUT")
            assert r.status == 200, await r.data

    async def test_wrong_specific_method(self) -> None:
        # To make sure the dummy server is actually returning failed responses
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            r = await pool.request("GET", "/specific_method", fields={"method": "POST"})
            assert r.status == 400, await r.data

        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            r = await pool.request("POST", "/specific_method", fields={"method": "GET"})
            assert r.status == 400, await r.data

    async def test_upload(self) -> None:
        data = "I'm in ur multipart form-data, hazing a cheezburgr"
        fields: dict[str, _TYPE_FIELD_VALUE_TUPLE] = {
            "upload_param": "filefield",
            "upload_filename": "lolcat.txt",
            "filefield": ("lolcat.txt", data),
        }
        fields["upload_size"] = len(data)  # type: ignore

        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            r = await pool.request("POST", "/upload", fields=fields)
            assert r.status == 200, await r.data

    async def test_one_name_multiple_values(self) -> None:
        fields = [("foo", "a"), ("foo", "b")]

        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            # urlencode
            r = await pool.request("GET", "/echo", fields=fields)
            assert await r.data == b"foo=a&foo=b"

            # multipart
            r = await pool.request("POST", "/echo", fields=fields)
            assert (await r.data).count(b'name="foo"') == 2

    async def test_request_method_body(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            body = b"hi"
            r = await pool.request("POST", "/echo", body=body)
            assert await r.data == body

            fields = [("hi", "hello")]
            with pytest.raises(TypeError):
                await pool.request("POST", "/echo", body=body, fields=fields)

    async def test_sending_async_iterable_orig_bytes(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:

            async def abody() -> typing.AsyncIterable[bytes]:
                await asyncio.sleep(0.01)
                yield b"foo"
                await asyncio.sleep(0.01)
                yield b"bar"

            r = await pool.request("POST", "/echo", body=abody())
            assert await r.data == b"foobar"

    async def test_sending_async_iterable_orig_str(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:

            async def abody() -> typing.AsyncIterable[str]:
                await asyncio.sleep(0.01)
                yield "foo"
                await asyncio.sleep(0.01)
                yield "bar"

            r = await pool.request("POST", "/echo", body=abody())
            assert await r.data == b"foobar"

    async def test_sending_async_iterable_orig_str_non_ascii(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:

            async def abody() -> typing.AsyncIterable[str]:
                await asyncio.sleep(0.01)
                yield "hélloà"
                await asyncio.sleep(0.01)
                yield "bar"

            r = await pool.request("POST", "/echo", body=abody())
            assert await r.data == "hélloàbar".encode()

    async def test_unicode_upload(self) -> None:
        fieldname = "myfile"
        filename = "\xe2\x99\xa5.txt"
        data = "\xe2\x99\xa5".encode()
        size = len(data)

        fields: dict[str, _TYPE_FIELD_VALUE_TUPLE] = {
            "upload_param": fieldname,
            "upload_filename": filename,
            fieldname: (filename, data),
        }
        fields["upload_size"] = size  # type: ignore
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            r = await pool.request("POST", "/upload", fields=fields)
            assert r.status == 200, await r.data

    async def test_nagle(self) -> None:
        """Test that connections have TCP_NODELAY turned on"""
        # This test needs to be here in order to be run. socket.create_connection actually tries
        # to connect to the host provided so we need a dummyserver to be running.
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            conn = await pool._get_conn()
            try:
                await pool._make_request(conn, "GET", "/")
                tcp_nodelay_setting = conn.sock.getsockopt(  # type: ignore[union-attr]
                    socket.IPPROTO_TCP, socket.TCP_NODELAY
                )
                assert tcp_nodelay_setting
            finally:
                await conn.close()

    @pytest.mark.parametrize(
        "socket_options",
        [
            [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)],
            ((socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),),
        ],
    )
    async def test_socket_options(self, socket_options: tuple[int, int, int]) -> None:
        """Test that connections accept socket options."""
        # This test needs to be here in order to be run. socket.create_connection actually tries to
        # connect to the host provided so we need a dummyserver to be running.
        async with AsyncHTTPConnectionPool(
            self.host,
            self.port,
            socket_options=socket_options,
        ) as pool:
            # Get the socket of a new connection.
            s = await (await pool._new_conn())._new_conn()
            try:
                using_keepalive = (
                    s.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) > 0
                )
                assert using_keepalive
            finally:
                s.close()

    async def test_defaults_are_applied(self) -> None:
        """Test that modifying the default socket options works."""
        # This test needs to be here in order to be run. socket.create_connection actually tries
        # to connect to the host provided so we need a dummyserver to be running.
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            # Get the HTTPConnection instance
            conn = await pool._new_conn()
            try:
                # Update the default socket options
                assert conn.socket_options is not None
                conn.socket_options += [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]  # type: ignore[operator]
                s = await conn._new_conn()
                nagle_disabled = (
                    s.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) > 0
                )
                using_keepalive = (
                    s.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) > 0
                )
                assert nagle_disabled
                assert using_keepalive
            finally:
                await conn.close()
                s.close()

    async def test_connection_error_retries(self) -> None:
        """ECONNREFUSED error should raise a connection error, with retries"""
        port = find_unused_port()
        async with AsyncHTTPConnectionPool(self.host, port) as pool:
            with pytest.raises(MaxRetryError) as e:
                await pool.request("GET", "/", retries=Retry(connect=3))
            assert type(e.value.reason) == NewConnectionError

    async def test_timeout_success(self) -> None:
        timeout = Timeout(connect=3, read=5, total=None)
        async with AsyncHTTPConnectionPool(
            self.host, self.port, timeout=timeout
        ) as pool:
            await pool.request("GET", "/")
            # This should not raise a "Timeout already started" error
            await pool.request("GET", "/")

        async with AsyncHTTPConnectionPool(
            self.host, self.port, timeout=timeout
        ) as pool:
            # This should also not raise a "Timeout already started" error
            await pool.request("GET", "/")

        timeout = Timeout(total=None)
        async with AsyncHTTPConnectionPool(
            self.host, self.port, timeout=timeout
        ) as pool:
            await pool.request("GET", "/")

    socket_timeout_reuse_testdata = pytest.mark.parametrize(
        ["timeout", "expect_settimeout_calls"],
        [
            (1, (1, 1)),
            (None, (None, None)),
            (Timeout(read=4), (None, 4)),
            (Timeout(read=4, connect=5), (5, 4)),
            (Timeout(connect=6), (6, None)),
        ],
    )

    @socket_timeout_reuse_testdata
    async def test_socket_timeout_updated_on_reuse_constructor(
        self,
        timeout: _TYPE_TIMEOUT,
        expect_settimeout_calls: typing.Sequence[float | None],
    ) -> None:
        async with AsyncHTTPConnectionPool(
            self.host, self.port, timeout=timeout
        ) as pool:
            # Make a request to create a new connection.
            await pool.urlopen("GET", "/")

            # Grab the connection and mock the inner socket.
            assert pool.pool is not None
            conn = await pool.pool.get_nowait()
            conn_sock = mock.Mock(wraps=conn.sock)  # type: ignore[union-attr]
            conn.sock = conn_sock  # type: ignore[union-attr]
            await pool._put_conn(conn)  # type: ignore[arg-type]

            # Assert that sock.settimeout() is called with the new connect timeout, then the read timeout.
            await pool.urlopen("GET", "/", timeout=timeout)
            conn_sock.settimeout.assert_has_calls(
                [mock.call(x) for x in expect_settimeout_calls]
            )

    @socket_timeout_reuse_testdata
    async def test_socket_timeout_updated_on_reuse_parameter(
        self,
        timeout: _TYPE_TIMEOUT,
        expect_settimeout_calls: typing.Sequence[float | None],
    ) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            # Make a request to create a new connection.
            await pool.urlopen("GET", "/", timeout=LONG_TIMEOUT)

            # Grab the connection and mock the inner socket.
            assert pool.pool is not None
            conn = await pool.pool.get_nowait()
            conn_sock = mock.Mock(wraps=conn.sock)  # type: ignore[union-attr]
            conn.sock = conn_sock  # type: ignore[union-attr]
            await pool._put_conn(conn)  # type: ignore[arg-type]

            # Assert that sock.settimeout() is called with the new connect timeout, then the read timeout.
            await pool.urlopen("GET", "/", timeout=timeout)
            conn_sock.settimeout.assert_has_calls(
                [mock.call(x) for x in expect_settimeout_calls]
            )

    async def test_tunnel(self) -> None:
        # note the actual httplib.py has no tests for this functionality
        timeout = Timeout(total=None)

        async with AsyncHTTPConnectionPool(
            self.host, self.port, timeout=timeout
        ) as pool:
            conn = await pool._get_conn()
            try:
                conn.set_tunnel(self.host, self.port)
                future: asyncio.Future[None] = asyncio.Future()
                future.set_result(None)
                with mock.patch.object(
                    conn, "_tunnel", create=True, return_value=future
                ) as conn_tunnel:
                    await pool._make_request(conn, "GET", "/")
                conn_tunnel.assert_called_once_with()
            finally:
                await conn.close()

        # test that it's not called when tunnel is not set
        timeout = Timeout(total=None)
        async with AsyncHTTPConnectionPool(
            self.host, self.port, timeout=timeout
        ) as pool:
            conn = await pool._get_conn()
            try:
                future = asyncio.Future()
                future.set_result(None)
                with mock.patch.object(
                    conn, "_tunnel", create=True, return_value=future
                ) as conn_tunnel:
                    await pool._make_request(conn, "GET", "/")
                assert not conn_tunnel.called
            finally:
                await conn.close()

    async def test_redirect_relative_url_no_deprecation(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            with warnings.catch_warnings():
                warnings.simplefilter("error", DeprecationWarning)
                await pool.request("GET", "/redirect", fields={"target": "/"})

    async def test_redirect(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            r = await pool.request(
                "GET", "/redirect", fields={"target": "/"}, redirect=False
            )
            assert r.status == 303

            r = await pool.request("GET", "/redirect", fields={"target": "/"})
            assert r.status == 200
            assert await r.data == b"Dummy server!"

    async def test_303_redirect_makes_request_lose_body(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            response = await pool.request(
                "POST",
                "/redirect",
                fields={"target": "/headers_and_params", "status": "303 See Other"},
            )
        data = await response.json()
        assert data["params"] == {}
        assert "Content-Type" not in HTTPHeaderDict(data["headers"])

    async def test_bad_connect(self) -> None:
        async with AsyncHTTPConnectionPool("badhost.invalid", self.port) as pool:
            with pytest.raises(MaxRetryError) as e:
                await pool.request("GET", "/", retries=5)
            assert type(e.value.reason) == NameResolutionError

    async def test_keepalive(self) -> None:
        async with AsyncHTTPConnectionPool(
            self.host, self.port, block=True, maxsize=1
        ) as pool:
            r = await pool.request("GET", "/keepalive?close=0")
            r = await pool.request("GET", "/keepalive?close=0")

            assert r.status == 200
            assert pool.num_connections == 1
            assert pool.num_requests == 2

    async def test_keepalive_close(self) -> None:
        async with AsyncHTTPConnectionPool(
            self.host, self.port, block=True, maxsize=1, timeout=2
        ) as pool:
            r = await pool.request(
                "GET", "/keepalive?close=1", retries=0, headers={"Connection": "close"}
            )

            assert pool.num_connections == 1

            # The dummyserver will have responded with Connection:close,
            # and httplib will properly cleanup the socket.

            # We grab the HTTPConnection object straight from the Queue,
            # because _get_conn() is where the check & reset occurs
            assert pool.pool is not None
            conn = await pool.pool.get()
            assert conn.sock is None  # type: ignore[union-attr]
            await pool._put_conn(conn)  # type: ignore[arg-type]

            # Now with keep-alive
            r = await pool.request(
                "GET",
                "/keepalive?close=0",
                retries=0,
                headers={"Connection": "keep-alive"},
            )

            # The dummyserver responded with Connection:keep-alive, the connection
            # persists.
            conn = await pool.pool.get()
            assert conn.sock is not None  # type: ignore[union-attr]
            await pool._put_conn(conn)  # type: ignore[arg-type]

            # Another request asking the server to close the connection. This one
            # should get cleaned up for the next request.
            r = await pool.request(
                "GET", "/keepalive?close=1", retries=0, headers={"Connection": "close"}
            )

            assert r.status == 200

            conn = await pool.pool.get()
            assert conn.sock is None  # type: ignore[union-attr]
            await pool._put_conn(conn)  # type: ignore[arg-type]

            # Next request
            r = await pool.request("GET", "/keepalive?close=0")

    async def test_post_with_urlencode(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            data = {"banana": "hammock", "lol": "cat"}
            r = await pool.request("POST", "/echo", fields=data, encode_multipart=False)
            assert (await r.data).decode("utf-8") == urlencode(data)

    async def test_post_with_multipart(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            data = {"banana": "hammock", "lol": "cat"}
            r = await pool.request("POST", "/echo", fields=data, encode_multipart=True)
            body = (await r.data).split(b"\r\n")

            encoded_data = encode_multipart_formdata(data)[0]
            expected_body = encoded_data.split(b"\r\n")

            # TODO: Get rid of extra parsing stuff when you can specify
            # a custom boundary to encode_multipart_formdata
            """
            We need to loop the return lines because a timestamp is attached
            from within encode_multipart_formdata. When the server echos back
            the data, it has the timestamp from when the data was encoded, which
            is not equivalent to when we run encode_multipart_formdata on
            the data again.
            """
            for i, line in enumerate(body):
                if line.startswith(b"--"):
                    continue

                assert body[i] == expected_body[i]

    async def test_post_with_multipart__iter__(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            data = {"hello": "world"}
            r = await pool.request(
                "POST",
                "/echo",
                fields=data,
                preload_content=False,
                multipart_boundary="boundary",
                encode_multipart=True,
            )

            chunks = [chunk async for chunk in r]
            assert chunks == [
                b"--boundary\r\n",
                b'Content-Disposition: form-data; name="hello"\r\n',
                b"\r\n",
                b"world\r\n",
                b"--boundary--\r\n",
            ]

    async def test_check_gzip(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            r = await pool.request(
                "GET", "/encodingrequest", headers={"accept-encoding": "gzip"}
            )
            assert r.headers.get("content-encoding") == "gzip"
            assert await r.data == b"hello, world!"

    async def test_check_deflate(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            r = await pool.request(
                "GET", "/encodingrequest", headers={"accept-encoding": "deflate"}
            )
            assert r.headers.get("content-encoding") == "deflate"
            assert await r.data == b"hello, world!"

    async def test_bad_decode(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            with pytest.raises(DecodeError):
                await pool.request(
                    "GET",
                    "/encodingrequest",
                    headers={"accept-encoding": "garbage-deflate"},
                )

            with pytest.raises(DecodeError):
                await pool.request(
                    "GET",
                    "/encodingrequest",
                    headers={"accept-encoding": "garbage-gzip"},
                )

    async def test_connection_count(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port, maxsize=1) as pool:
            await pool.request("GET", "/")
            await pool.request("GET", "/")
            await pool.request("GET", "/")

            assert pool.num_connections == 1
            assert pool.num_requests == 3

    async def test_connection_count_bigpool(self) -> None:
        async with AsyncHTTPConnectionPool(
            self.host, self.port, maxsize=16
        ) as http_pool:
            await http_pool.request("GET", "/")
            await http_pool.request("GET", "/")
            await http_pool.request("GET", "/")

            assert http_pool.num_connections == 1
            assert http_pool.num_requests == 3

    async def test_partial_response(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port, maxsize=1) as pool:
            req_data = {"lol": "cat"}
            resp_data = urlencode(req_data).encode("utf-8")

            r = await pool.request(
                "GET", "/echo", fields=req_data, preload_content=False
            )

            assert await r.read(5) == resp_data[:5]
            assert await r.read() == resp_data[5:]

    async def test_lazy_load_twice(self) -> None:
        # This test is sad and confusing. Need to figure out what's
        # going on with partial reads and socket reuse.

        async with AsyncHTTPConnectionPool(
            self.host, self.port, block=True, maxsize=1, timeout=2
        ) as pool:
            payload_size = 1024 * 2
            first_chunk = 512

            boundary = "foo"

            req_data = {"count": "a" * payload_size}
            resp_data = encode_multipart_formdata(req_data, boundary=boundary)[0]

            req2_data = {"count": "b" * payload_size}
            resp2_data = encode_multipart_formdata(req2_data, boundary=boundary)[0]

            r1 = await pool.request(
                "POST",
                "/echo",
                fields=req_data,
                multipart_boundary=boundary,
                preload_content=False,
            )

            assert await r1.read(first_chunk) == resp_data[:first_chunk]

            return

            try:
                r2 = await pool.request(
                    "POST",
                    "/echo",
                    fields=req2_data,
                    multipart_boundary=boundary,
                    preload_content=False,
                    pool_timeout=0.001,
                )

                # This branch should generally bail here, but maybe someday it will
                # work? Perhaps by some sort of magic. Consider it a TODO.

                assert await r2.read(first_chunk) == resp2_data[:first_chunk]

                assert await r1.read() == resp_data[first_chunk:]
                assert await r2.read() == resp2_data[first_chunk:]
                assert pool.num_requests == 2

            except EmptyPoolError:
                assert await r1.read() == resp_data[first_chunk:]
                assert pool.num_requests == 1

            assert pool.num_connections == 1

    async def test_release_conn_parameter(self) -> None:
        MAXSIZE = 5
        async with AsyncHTTPConnectionPool(
            self.host, self.port, maxsize=MAXSIZE
        ) as pool:
            assert pool.pool is not None
            assert pool.pool.qsize() == 0

            # Make request without releasing connection
            await pool.request("GET", "/", release_conn=False, preload_content=False)
            assert pool.pool.qsize() == 0

    async def test_dns_error(self) -> None:
        async with AsyncHTTPConnectionPool(
            "thishostdoesnotexist.invalid", self.port, timeout=0.001
        ) as pool:
            with pytest.raises(MaxRetryError):
                await pool.request("GET", "/test", retries=2)

    @pytest.mark.parametrize("char", [" ", "\r", "\n", "\x00"])
    async def test_invalid_method_not_allowed(self, char: str) -> None:
        with pytest.raises(ValueError):
            async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
                await pool.request("GET" + char, "/")

    async def test_percent_encode_invalid_target_chars(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            r = await pool.request("GET", "/echo_params?q=\r&k=\n \n")
            assert await r.data == b"[('k', '\\n \\n'), ('q', '\\r')]"

    async def test_source_address(self) -> None:
        for addr, is_ipv6 in VALID_SOURCE_ADDRESSES:
            if is_ipv6 and not HAS_IPV6_AND_DNS:
                warnings.warn("No IPv6 support: skipping.", NoIPv6Warning)
                continue
            async with AsyncHTTPConnectionPool(
                self.host, self.port, source_address=addr, retries=False
            ) as pool:
                r = await pool.request("GET", "/source_address")
                assert await r.data == addr[0].encode()

    @pytest.mark.parametrize(
        "invalid_source_address, is_ipv6", INVALID_SOURCE_ADDRESSES
    )
    async def test_source_address_error(
        self, invalid_source_address: tuple[str, int], is_ipv6: bool
    ) -> None:
        async with AsyncHTTPConnectionPool(
            self.host, self.port, source_address=invalid_source_address, retries=False
        ) as pool:
            if is_ipv6:
                with pytest.raises(NameResolutionError):
                    await pool.request(
                        "GET", f"/source_address?{invalid_source_address}"
                    )
            else:
                with pytest.raises(NewConnectionError):
                    await pool.request(
                        "GET", f"/source_address?{invalid_source_address}"
                    )

    async def test_stream_keepalive(self) -> None:
        x = 2

        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            for _ in range(x):
                response = await pool.request(
                    "GET",
                    "/chunked",
                    headers={"Connection": "keep-alive"},
                    preload_content=False,
                    retries=False,
                )
                async for chunk in response.stream(amt=3):
                    assert chunk == b"123"

            assert pool.num_connections == 1
            assert pool.num_requests == x

    async def test_chunked_gzip(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            response = await pool.request(
                "GET", "/chunked_gzip", preload_content=False, decode_content=True
            )

            assert b"123" * 4 == await response.read()

    async def test_cleanup_on_connection_error(self) -> None:
        """
        Test that connections are recycled to the pool on
        connection errors where no http response is received.
        """
        poolsize = 3
        async with AsyncHTTPConnectionPool(
            self.host, self.port, maxsize=poolsize, block=True
        ) as http:
            assert http.pool is not None
            assert http.pool.qsize() == 0

            # force a connection error by supplying a non-existent
            # url. We won't get a response for this  and so the
            # conn won't be implicitly returned to the pool.
            with pytest.raises(MaxRetryError):
                await http.request(
                    "GET",
                    "/redirect",
                    fields={"target": "/"},
                    release_conn=False,
                    retries=0,
                )

            r = await http.request(
                "GET",
                "/redirect",
                fields={"target": "/"},
                release_conn=False,
                retries=1,
            )
            r.release_conn()

            # the pool should still contain poolsize elements
            assert http.pool.qsize() == 1

    async def test_mixed_case_hostname(self) -> None:
        async with AsyncHTTPConnectionPool("LoCaLhOsT", self.port) as pool:
            response = await pool.request("GET", f"http://LoCaLhOsT:{self.port}/")
            assert response.status == 200

    async def test_preserves_path_dot_segments(self) -> None:
        """ConnectionPool preserves dot segments in the URI"""
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            response = await pool.request("GET", "/echo_uri/seg0/../seg2")
            assert await response.data == b"/echo_uri/seg0/../seg2"

    async def test_default_user_agent_header(self) -> None:
        """ConnectionPool has a default user agent"""
        default_ua = _get_default_user_agent()
        custom_ua = "I'm not a web scraper, what are you talking about?"
        custom_ua2 = "Yet Another User Agent"
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            # Use default user agent if no user agent was specified.
            r = await pool.request("GET", "/headers")
            request_headers = await r.json()
            assert request_headers.get("User-Agent") == _get_default_user_agent()

            # Prefer the request user agent over the default.
            headers = {"UsEr-AGENt": custom_ua}
            r = await pool.request("GET", "/headers", headers=headers)
            request_headers = await r.json()
            assert request_headers.get("User-Agent") == custom_ua

            # Do not modify pool headers when using the default user agent.
            pool_headers = {"foo": "bar"}
            pool.headers = pool_headers
            r = await pool.request("GET", "/headers")
            request_headers = await r.json()
            assert request_headers.get("User-Agent") == default_ua
            assert "User-Agent" not in pool_headers

            pool.headers.update({"User-Agent": custom_ua2})
            r = await pool.request("GET", "/headers")
            request_headers = await r.json()
            assert request_headers.get("User-Agent") == custom_ua2

    @pytest.mark.parametrize(
        "headers",
        [
            None,
            {},
            {"User-Agent": "key"},
            {"user-agent": "key"},
            {b"uSeR-AgEnT": b"key"},
            {b"user-agent": "key"},
        ],
    )
    @pytest.mark.parametrize("chunked", [True, False])
    async def test_user_agent_header_not_sent_twice(
        self, headers: dict[str, str] | None, chunked: bool
    ) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            r = await pool.request("GET", "/headers", headers=headers, chunked=chunked)
            request_headers = await r.json()

            if not headers:
                assert request_headers["User-Agent"].startswith("urllib3.future/")
                assert "key" not in request_headers["User-Agent"]
            else:
                assert request_headers["User-Agent"] == "key"

    async def test_no_user_agent_header(self) -> None:
        """ConnectionPool can suppress sending a user agent header"""
        custom_ua = "I'm not a web scraper, what are you talking about?"
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            # Suppress user agent in the request headers.
            no_ua_headers = {"User-Agent": SKIP_HEADER}
            r = await pool.request("GET", "/headers", headers=no_ua_headers)
            request_headers = await r.json()
            assert "User-Agent" not in request_headers
            assert no_ua_headers["User-Agent"] == SKIP_HEADER

            # Suppress user agent in the pool headers.
            pool.headers = no_ua_headers
            r = await pool.request("GET", "/headers")
            request_headers = await r.json()
            assert "User-Agent" not in request_headers
            assert no_ua_headers["User-Agent"] == SKIP_HEADER

            # Request headers override pool headers.
            pool_headers = {"User-Agent": custom_ua}
            pool.headers = pool_headers
            r = await pool.request("GET", "/headers", headers=no_ua_headers)
            request_headers = await r.json()
            assert "User-Agent" not in request_headers
            assert no_ua_headers["User-Agent"] == SKIP_HEADER
            assert pool_headers.get("User-Agent") == custom_ua

    @pytest.mark.parametrize(
        "accept_encoding",
        [
            "Accept-Encoding",
            "accept-encoding",
            b"Accept-Encoding",
            b"accept-encoding",
            None,
        ],
    )
    @pytest.mark.parametrize(
        "user_agent", ["User-Agent", "user-agent", b"User-Agent", b"user-agent", None]
    )
    @pytest.mark.parametrize("chunked", [True, False])
    async def test_skip_header(
        self,
        accept_encoding: str | None,
        user_agent: str | None,
        chunked: bool,
    ) -> None:
        headers = {}

        if accept_encoding is not None:
            headers[accept_encoding] = SKIP_HEADER

        if user_agent is not None:
            headers[user_agent] = SKIP_HEADER

        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            r = await pool.request("GET", "/headers", headers=headers, chunked=chunked)
        request_headers = await r.json()

        if accept_encoding is None:
            assert "Accept-Encoding" in request_headers
        else:
            assert accept_encoding not in request_headers

        if user_agent is None:
            assert "User-Agent" in request_headers
        else:
            assert user_agent not in request_headers

    @pytest.mark.parametrize("header", ["Content-Length", "content-length"])
    @pytest.mark.parametrize("chunked", [True, False])
    async def test_skip_header_non_supported(self, header: str, chunked: bool) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            with pytest.raises(
                ValueError,
                match="urllib3.util.SKIP_HEADER only supports 'Accept-Encoding', 'Host', 'User-Agent'",
            ) as e:
                await pool.request(
                    "GET", "/headers", headers={header: SKIP_HEADER}, chunked=chunked
                )
            # Ensure that the error message stays up to date with 'SKIP_HEADER_SUPPORTED_HEADERS'
            assert all(
                ("'" + header.title() + "'") in str(e.value)
                for header in SKIPPABLE_HEADERS
            )

    @pytest.mark.parametrize("chunked", [True, False])
    @pytest.mark.parametrize("pool_request", [True, False])
    @pytest.mark.parametrize("header_type", [dict, HTTPHeaderDict])
    async def test_headers_not_modified_by_request(
        self,
        chunked: bool,
        pool_request: bool,
        header_type: type[dict[str, str] | HTTPHeaderDict],
    ) -> None:
        # Test that the .request*() methods of ConnectionPool and HTTPConnection
        # don't modify the given 'headers' structure, instead they should
        # make their own internal copies at request time.
        headers = header_type()
        headers["key"] = "val"

        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            pool.headers = headers
            if pool_request:
                await pool.request("GET", "/headers", chunked=chunked)
            else:
                conn = await pool._get_conn()
                await conn.request("GET", "/headers", chunked=chunked)

            assert pool.headers == {"key": "val"}
            assert isinstance(pool.headers, header_type)

        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            if pool_request:
                await pool.request("GET", "/headers", headers=headers, chunked=chunked)
            else:
                conn = await pool._get_conn()
                await conn.request("GET", "/headers", headers=headers, chunked=chunked)

            assert headers == {"key": "val"}

    async def test_bytes_header(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            headers = {"User-Agent": "test header"}
            r = await pool.request("GET", "/headers", headers=headers)
            request_headers = await r.json()
            assert "User-Agent" in request_headers
            assert request_headers["User-Agent"] == "test header"

    @pytest.mark.parametrize(
        "user_agent, should_encode",
        [("Schönefeld/1.18.0", False), ("Schönefeld/1.18.0", True)],
    )
    async def test_user_agent_non_ascii_user_agent(
        self, user_agent: str, should_encode: bool
    ) -> None:
        ua_value: str | bytes

        async with AsyncHTTPConnectionPool(self.host, self.port, retries=False) as pool:
            if should_encode is False:
                ua_value = user_agent
            else:
                ua_value = user_agent.encode("iso-8859-1")
            r = await pool.urlopen(
                "GET",
                "/headers",
                headers={"User-Agent": ua_value},  # type: ignore[dict-item]
            )
            request_headers = await r.json()
            assert "User-Agent" in request_headers
            assert request_headers["User-Agent"] == "Schönefeld/1.18.0"

    async def test_fake_multiplexed_connection(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port, maxsize=3) as pool:
            promise_a = await pool.request("GET", "/headers", multiplexed=True)
            assert not isinstance(promise_a, ResponsePromise)
            assert pool.num_connections == 1
            promise_b = await pool.request("GET", "/headers", multiplexed=True)
            assert not isinstance(promise_b, ResponsePromise)
            assert pool.num_connections == 1


@pytest.mark.asyncio
class TestRetry(HTTPDummyServerTestCase):
    async def test_max_retry(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            with pytest.raises(MaxRetryError):
                await pool.request(
                    "GET", "/redirect", fields={"target": "/"}, retries=0
                )

    async def test_disabled_retry(self) -> None:
        """Disabled retries should disable redirect handling."""
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            r = await pool.request(
                "GET", "/redirect", fields={"target": "/"}, retries=False
            )
            assert r.status == 303

            r = await pool.request(
                "GET",
                "/redirect",
                fields={"target": "/"},
                retries=Retry(redirect=False),
            )
            assert r.status == 303

        async with AsyncHTTPConnectionPool(
            "thishostdoesnotexist.invalid", self.port, timeout=0.001
        ) as pool:
            with pytest.raises(NameResolutionError):
                await pool.request("GET", "/test", retries=False)

    async def test_read_retries(self) -> None:
        """Should retry for status codes in the forcelist"""
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            retry = Retry(read=1, status_forcelist=[418])
            resp = await pool.request(
                "GET",
                "/successful_retry",
                headers={"test-name": "async_test_read_retries"},
                retries=retry,
            )
            assert resp.status == 200

    async def test_read_total_retries(self) -> None:
        """HTTP response w/ status code in the forcelist should be retried"""
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            headers = {"test-name": "async_test_read_total_retries"}
            retry = Retry(total=1, status_forcelist=[418])
            resp = await pool.request(
                "GET", "/successful_retry", headers=headers, retries=retry
            )
            assert resp.status == 200

    async def test_retries_wrong_forcelist(self) -> None:
        """HTTP response w/ status code not in forcelist shouldn't be retried"""
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            retry = Retry(total=1, status_forcelist=[202])
            resp = await pool.request(
                "GET",
                "/successful_retry",
                headers={"test-name": "async_test_wrong_forcelist"},
                retries=retry,
            )
            assert resp.status == 418

    async def test_default_method_forcelist_retried(self) -> None:
        """urllib3 should retry methods in the default method forcelist"""
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            retry = Retry(total=1, status_forcelist=[418])
            resp = await pool.request(
                "OPTIONS",
                "/successful_retry",
                headers={"test-name": "async_test_default_forcelist"},
                retries=retry,
            )
            assert resp.status == 200

    async def test_retries_wrong_method_list(self) -> None:
        """Method not in our allowed list should not be retried, even if code matches"""
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            headers = {"test-name": "async_test_wrong_allowed_method"}
            retry = Retry(total=1, status_forcelist=[418], allowed_methods=["POST"])
            resp = await pool.request(
                "GET", "/successful_retry", headers=headers, retries=retry
            )
            assert resp.status == 418

    async def test_read_retries_unsuccessful(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            headers = {"test-name": "async_test_read_retries_unsuccessful"}
            resp = await pool.request(
                "GET", "/successful_retry", headers=headers, retries=1
            )
            assert resp.status == 418

    async def test_retry_reuse_safe(self) -> None:
        """It should be possible to reuse a Retry object across requests"""
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            headers = {"test-name": "async_test_retry_safe"}
            retry = Retry(total=1, status_forcelist=[418])
            resp = await pool.request(
                "GET", "/successful_retry", headers=headers, retries=retry
            )
            assert resp.status == 200

        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            resp = await pool.request(
                "GET", "/successful_retry", headers=headers, retries=retry
            )
            assert resp.status == 200

    async def test_retry_return_in_response(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            headers = {"test-name": "async_test_retry_return_in_response"}
            retry = Retry(total=2, status_forcelist=[418])
            resp = await pool.request(
                "GET", "/successful_retry", headers=headers, retries=retry
            )
            assert resp.status == 200
            assert resp.retries is not None
            assert resp.retries.total == 1
            assert resp.retries.history == (
                RequestHistory("GET", "/successful_retry", None, 418, None),
            )

    async def test_retry_redirect_history(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            resp = await pool.request("GET", "/redirect", fields={"target": "/"})
            assert resp.status == 200
            assert resp.retries is not None
            assert resp.retries.history == (
                RequestHistory("GET", "/redirect?target=%2F", None, 303, "/"),
            )

    async def test_multi_redirect_history(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            r = await pool.request(
                "GET",
                "/multi_redirect",
                fields={"redirect_codes": "303,302,200"},
                redirect=False,
            )
            assert r.status == 303
            assert r.retries is not None
            assert r.retries.history == tuple()

        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            r = await pool.request(
                "GET",
                "/multi_redirect",
                retries=10,
                fields={"redirect_codes": "303,302,301,307,302,200"},
            )
            assert r.status == 200
            assert await r.data == b"Done redirecting"

            expected = [
                (303, "/multi_redirect?redirect_codes=302,301,307,302,200"),
                (302, "/multi_redirect?redirect_codes=301,307,302,200"),
                (301, "/multi_redirect?redirect_codes=307,302,200"),
                (307, "/multi_redirect?redirect_codes=302,200"),
                (302, "/multi_redirect?redirect_codes=200"),
            ]
            assert r.retries is not None
            actual = [
                (history.status, history.redirect_location)
                for history in r.retries.history
            ]
            assert actual == expected


@pytest.mark.asyncio
class TestRetryAfter(HTTPDummyServerTestCase):
    async def test_retry_after(self) -> None:
        # Request twice in a second to get a 429 response.
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            r = await pool.request(
                "GET",
                "/retry_after",
                fields={"status": "429 Too Many Requests"},
                retries=False,
            )
            r = await pool.request(
                "GET",
                "/retry_after",
                fields={"status": "429 Too Many Requests"},
                retries=False,
            )
            assert r.status == 429

            r = await pool.request(
                "GET",
                "/retry_after",
                fields={"status": "429 Too Many Requests"},
                retries=True,
            )
            assert r.status == 200

            # Request twice in a second to get a 503 response.
            r = await pool.request(
                "GET",
                "/retry_after",
                fields={"status": "503 Service Unavailable"},
                retries=False,
            )
            r = await pool.request(
                "GET",
                "/retry_after",
                fields={"status": "503 Service Unavailable"},
                retries=False,
            )
            assert r.status == 503

            r = await pool.request(
                "GET",
                "/retry_after",
                fields={"status": "503 Service Unavailable"},
                retries=True,
            )
            assert r.status == 200

            # Ignore Retry-After header on status which is not defined in
            # Retry.RETRY_AFTER_STATUS_CODES.
            r = await pool.request(
                "GET",
                "/retry_after",
                fields={"status": "418 I'm a teapot"},
                retries=True,
            )
            assert r.status == 418

    async def test_redirect_after(self) -> None:
        async with AsyncHTTPConnectionPool(self.host, self.port) as pool:
            r = await pool.request("GET", "/redirect_after", retries=False)
            assert r.status == 303

            t = time.time()
            r = await pool.request("GET", "/redirect_after")
            assert r.status == 200
            delta = time.time() - t
            assert delta >= 1

            t = time.time()
            timestamp = t + 2
            r = await pool.request("GET", "/redirect_after?date=" + str(timestamp))
            assert r.status == 200
            delta = time.time() - t
            assert delta >= 1

            # Retry-After is past
            t = time.time()
            timestamp = t - 1
            r = await pool.request("GET", "/redirect_after?date=" + str(timestamp))
            delta = time.time() - t
            assert r.status == 200
            assert delta < 1


@pytest.mark.asyncio
class TestFileBodiesOnRetryOrRedirect(HTTPDummyServerTestCase):
    async def test_retries_put_filehandle(self) -> None:
        """HTTP PUT retry with a file-like object should not timeout"""
        async with AsyncHTTPConnectionPool(self.host, self.port, timeout=0.1) as pool:
            retry = Retry(total=3, status_forcelist=[418])
            # httplib reads in 8k chunks; use a larger content length
            content_length = 65535
            data = b"A" * content_length
            uploaded_file = io.BytesIO(data)
            headers = {
                "test-name": "async_test_retries_put_filehandle",
                "Content-Length": str(content_length),
            }
            resp = await pool.urlopen(
                "PUT",
                "/successful_retry",
                headers=headers,
                retries=retry,
                body=uploaded_file,
                assert_same_host=False,
                redirect=False,
            )
            assert resp.status == 200

    async def test_redirect_put_file(self) -> None:
        """PUT with file object should work with a redirection response"""
        async with AsyncHTTPConnectionPool(self.host, self.port, timeout=0.1) as pool:
            retry = Retry(total=3, status_forcelist=[418])
            # httplib reads in 8k chunks; use a larger content length
            content_length = 65535
            data = b"A" * content_length
            uploaded_file = io.BytesIO(data)
            headers = {
                "test-name": "async_test_redirect_put_file",
                "Content-Length": str(content_length),
            }
            url = "/redirect?target=/echo&status=307"
            resp = await pool.urlopen(
                "PUT",
                url,
                headers=headers,
                retries=retry,
                body=uploaded_file,
                assert_same_host=False,
                redirect=True,
            )
            assert resp.status == 200
            assert await resp.data == data

    async def test_redirect_with_failed_tell(self) -> None:
        """Abort request if failed to get a position from tell()"""

        class BadTellObject(io.BytesIO):
            def tell(self) -> typing.NoReturn:
                raise OSError

        body = BadTellObject(b"the data")
        url = "/redirect?target=/successful_retry"
        # httplib uses fileno if Content-Length isn't supplied,
        # which is unsupported by BytesIO.
        headers = {"Content-Length": "8"}
        async with AsyncHTTPConnectionPool(self.host, self.port, timeout=0.1) as pool:
            with pytest.raises(
                UnrewindableBodyError, match="Unable to record file position for"
            ):
                await pool.urlopen("PUT", url, headers=headers, body=body)


@pytest.mark.asyncio
class TestRetryPoolSize(HTTPDummyServerTestCase):
    async def test_pool_size_retry(self) -> None:
        retries = Retry(total=1, raise_on_status=False, status_forcelist=[404])
        async with AsyncHTTPConnectionPool(
            self.host, self.port, maxsize=10, retries=retries, block=True
        ) as pool:
            await pool.urlopen("GET", "/not_found", preload_content=False)
            assert pool.num_connections == 1


@pytest.mark.asyncio
class TestRedirectPoolSize(HTTPDummyServerTestCase):
    async def test_pool_size_redirect(self) -> None:
        retries = Retry(
            total=1, raise_on_status=False, status_forcelist=[404], redirect=True
        )
        async with AsyncHTTPConnectionPool(
            self.host, self.port, maxsize=10, retries=retries, block=True
        ) as pool:
            await pool.urlopen("GET", "/redirect", preload_content=False)
            assert pool.num_connections == 1
