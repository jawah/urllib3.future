from __future__ import annotations

import asyncio
import errno
import inspect
import socket
import ssl
import time
from threading import Event
from typing import Any

import pytest

from dummyserver.server import DEFAULT_CA
from dummyserver.testcase import HTTPDummyServerTestCase, HTTPSDummyServerTestCase
from urllib3 import (
    AsyncHTTPConnectionPool,
    AsyncHTTPSConnectionPool,
    HTTPConnectionPool,
    HTTPSConnectionPool,
    HttpVersion,
)
from urllib3.connection import HTTPConnection
from urllib3.contrib.resolver.in_memory import InMemoryResolver
from urllib3.contrib.resolver._async.in_memory import (
    InMemoryResolver as AsyncInMemoryResolver,
)
from urllib3.util.traffic_police import UnavailableTraffic
from urllib3.exceptions import (
    ClosedPoolError,
    ProtocolError,
    ReadTimeoutError,
    SSLError,
)
from .test_http2_lifecycle import HTTP2Peer, http2_peer as http2_peer


async def call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    result = fn(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


class PoolChecks:
    def make_pool(self: Any, asynchronous: bool, **kwargs: Any) -> Any:
        tls = self.scheme == "https"
        cls = (
            (AsyncHTTPSConnectionPool if asynchronous else HTTPSConnectionPool)
            if tls
            else (AsyncHTTPConnectionPool if asynchronous else HTTPConnectionPool)
        )
        if tls:
            kwargs["ca_certs"] = DEFAULT_CA
        return cls(
            self.host,
            self.port,
            timeout=5,
            retries=False,
            background_watch_delay=None,
            disabled_svn={HttpVersion.h2, HttpVersion.h3},
            **kwargs,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("asynchronous", [False, True])
    async def test_direct_checkout_happy_eyeballs_preserves_options(
        self: Any, asynchronous: bool
    ) -> None:
        ech = b"\x00\x05\xff\xff\x00\x01x"
        resolver = (AsyncInMemoryResolver if asynchronous else InMemoryResolver)(
            "localhost:127.0.0.2", "localhost:127.0.0.1", ech_config=ech
        )
        cache: dict[Any, Any] = {}
        pool = self.make_pool(
            asynchronous,
            resolver=resolver,
            happy_eyeballs=True,
            source_address=("127.0.0.1", 0),
            preemptive_quic_cache=cache,
        )
        conn = None
        try:
            conn = await call(pool._get_conn)
            assert conn.sock is not None
            raw_socket = conn.sock._sock if asynchronous else conn.sock
            assert raw_socket.getsockname()[0] == "127.0.0.1"
            records = await call(
                conn._resolver.getaddrinfo,
                "localhost",
                self.port,
                socket.AF_INET,
                socket.SOCK_STREAM,
            )
            assert records[0][3] == ech
            assert conn._preemptive_quic_cache is cache
            await call(conn.request, "GET", "/")
            response = await call(conn.getresponse)
            assert response.status == 200
            await call(response.read)
            await call(pool._put_conn, conn)
            assert self.host in repr(pool)
        finally:
            await call(pool.close)
        assert "Closed" in repr(pool)
        with pytest.raises(ClosedPoolError):
            await call(pool._get_conn)
        assert conn is not None
        await call(pool._put_conn, conn)
        assert conn.sock is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("asynchronous", [False, True])
    @pytest.mark.parametrize("phase", ["response", "promise", "error", "unavailable"])
    async def test_close_between_request_and_result(
        self: Any, asynchronous: bool, phase: str
    ) -> None:
        pool = self.make_pool(asynchronous)
        base: Any = pool.ConnectionCls

        async def async_request(conn: Any, *args: Any, **kwargs: Any) -> Any:
            result = await base.request(conn, *args, **kwargs)
            await pool.close()
            if phase == "unavailable":
                raise UnavailableTraffic("pool was closed")
            if phase == "error":
                raise ValueError("backend state disappeared")
            return result

        def sync_request(conn: Any, *args: Any, **kwargs: Any) -> Any:
            result = base.request(conn, *args, **kwargs)
            pool.close()
            if phase == "unavailable":
                raise UnavailableTraffic("pool was closed")
            if phase == "error":
                raise ValueError("backend state disappeared")
            return result

        pool.ConnectionCls = type(
            "ClosingConnection",
            (base,),
            {
                "request": async_request if asynchronous else sync_request,
            },
        )
        try:
            with pytest.raises(ClosedPoolError):
                await call(pool.request, "GET", "/", multiplexed=phase == "promise")
        finally:
            await call(pool.close)


class TestHTTPPoolEdges(PoolChecks, HTTPDummyServerTestCase):
    @pytest.mark.asyncio
    async def test_nonblocking_checkout_recovers_after_wait_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pool = self.make_pool(True, block=False, maxsize=1)
        entered, release = asyncio.Event(), asyncio.Event()
        original = pool._new_conn

        async def hold_connection() -> None:
            async with pool.pool.borrow():
                entered.set()
                await release.wait()

        holder = None
        try:
            assert (await pool.request("GET", "/")).status == 200
            holder = asyncio.create_task(hold_connection())
            await asyncio.wait_for(entered.wait(), 5)

            async def new_after_timeout(**kwargs: Any) -> Any:
                release.set()
                await holder
                return await original(**kwargs)

            monkeypatch.setattr(pool, "_new_conn", new_after_timeout)
            conn = await asyncio.wait_for(pool._get_conn(timeout=0.01), 5)
            await conn.request("GET", "/")
            response = await conn.getresponse()
            assert response.status == 200
            await response.read()
            await pool._put_conn(conn)
        finally:
            release.set()
            if holder is not None:
                await holder
            await pool.close()

    def test_happy_eyeballs_closes_a_connected_loser(self) -> None:
        connected, finish, stopped = Event(), Event(), Event()
        created: list[Any] = []

        class Resolver(InMemoryResolver):
            def getaddrinfo(self, *args: Any, **kwargs: Any) -> Any:
                return super().getaddrinfo(*args, **kwargs) * 2

        class Connection(HTTPConnection):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                created.append(self)

            def connect(self) -> None:
                if self is not created[0]:
                    assert connected.wait(5)
                super().connect()
                if self is created[0]:
                    connected.set()
                    try:
                        assert finish.wait(10)
                    finally:
                        stopped.set()

        resolver = Resolver("localhost:127.0.0.1")
        pool = self.make_pool(False, resolver=resolver, happy_eyeballs=True)
        pool.ConnectionCls = Connection
        try:
            response = pool.request("GET", "/")
            assert response.status == 200
            assert len(created) == 2 and created[0].sock is None
        finally:
            finish.set()
            assert stopped.wait(5)
            pool.close()


class TestHTTPSPoolEdges(PoolChecks, HTTPSDummyServerTestCase):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("http2_peer", ["ignore-ping"], indirect=True)
async def test_checkout_retires_an_unanswered_ping(
    http2_peer: HTTP2Peer, asynchronous: bool
) -> None:
    pool: Any = (AsyncHTTPSConnectionPool if asynchronous else HTTPSConnectionPool)(
        "localhost",
        http2_peer.port,
        ca_certs=DEFAULT_CA,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        disabled_svn={HttpVersion.h11, HttpVersion.h3},
        timeout=5,
        retries=False,
        background_watch_delay=None,
    )
    try:
        assert (await call(pool.request, "GET", "/")).status == 200
        conn = next(iter(pool.pool._registry.values()))
        old_socket = conn.sock
        await call(conn.ping)
        await asyncio.get_running_loop().run_in_executor(
            None, http2_peer.pings.get, True, 5
        )
        assert (await call(pool.request, "GET", "/next")).status == 200
        assert conn.sock is not old_socket
    finally:
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("blind", [False, True])
@pytest.mark.parametrize("failure", ["socket", "ssl", "would-block", "timeout"])
async def test_deferred_response_errors_are_normalized(
    http2_peer: HTTP2Peer,
    asynchronous: bool,
    blind: bool,
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool: Any = (AsyncHTTPSConnectionPool if asynchronous else HTTPSConnectionPool)(
        "localhost",
        http2_peer.port,
        ca_certs=DEFAULT_CA,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        disabled_svn={HttpVersion.h11, HttpVersion.h3},
        timeout=5,
        retries=False,
        background_watch_delay=None,
    )
    error = {
        "socket": OSError("injected read failure"),
        "ssl": ssl.SSLError("bad record"),
        "would-block": BlockingIOError(errno.EAGAIN, "injected would-block"),
        "timeout": socket.timeout("timed out"),
    }[failure]
    expected = {
        "socket": ProtocolError,
        "ssl": SSLError,
        "would-block": ReadTimeoutError,
        "timeout": ReadTimeoutError,
    }[failure]
    conn = None
    try:
        promise = await call(pool.request, "GET", "/", multiplexed=True)
        conn = next(iter(pool.pool._registry.values()))
        sock = conn.sock
        receive = type(sock).recv
        failed = False

        def fail_once(current: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal failed
            if current is sock and not failed:
                failed = True
                raise error
            return receive(current, *args, **kwargs)

        with monkeypatch.context() as io:
            io.setattr(type(sock), "recv", fail_once)
            with pytest.raises(expected):
                await call(pool.get_response, **({} if blind else {"promise": promise}))
        assert failed
        # The injected exception did not consume or alter the actual peer reply.
        assert (await call(pool.get_response, promise=promise)).status == 200
    finally:
        if conn is not None:
            await call(conn.close)
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "failure",
    ["close-promise", "read-timeout", "read-error", "write-error", "write-eprototype"],
)
async def test_pool_error_boundaries_after_sending_request(
    http2_peer: HTTP2Peer,
    asynchronous: bool,
    failure: str,
) -> None:
    pool: Any = (AsyncHTTPSConnectionPool if asynchronous else HTTPSConnectionPool)(
        "localhost",
        http2_peer.port,
        ca_certs=DEFAULT_CA,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        disabled_svn={HttpVersion.h11, HttpVersion.h3},
        timeout=5,
        retries=False,
        background_watch_delay=None,
    )
    base: Any = pool.ConnectionCls
    observed = False

    async def after_request(conn: Any, promise: Any) -> None:
        nonlocal observed
        # Wait for actual peer input before injecting the failure. This also
        # prevents tearing down TLS while the server is still sending its reply.
        deadline = time.monotonic() + 5
        while not conn._protocol.has_pending_event(stream_id=promise.stream_id):
            assert time.monotonic() < deadline, "Peer response did not arrive"
            await call(conn.peek_and_react, expect_frame=True)
        observed = True
        if failure == "close-promise":
            await call(pool.close)
        elif failure.startswith("write-"):
            raise OSError(
                errno.EPROTOTYPE if failure == "write-eprototype" else errno.EIO,
                "injected write failure",
            )

    def sync_request(conn: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal observed
        result = base.request(conn, *args, **kwargs)
        deadline = time.monotonic() + 5
        while not conn._protocol.has_pending_event(stream_id=result.stream_id):
            assert time.monotonic() < deadline, "Peer response did not arrive"
            conn.peek_and_react(expect_frame=True)
        observed = True
        if failure == "close-promise":
            pool.close()
        elif failure.startswith("write-"):
            raise OSError(
                errno.EPROTOTYPE if failure == "write-eprototype" else errno.EIO,
                "injected write failure",
            )
        return result

    async def async_request(conn: Any, *args: Any, **kwargs: Any) -> Any:
        result = await base.request(conn, *args, **kwargs)
        await after_request(conn, result)
        return result

    def read_error() -> None:
        if failure == "read-timeout":
            raise socket.timeout("injected read timeout")
        if failure == "read-error":
            raise OSError(errno.EIO, "injected read failure")

    def sync_getresponse(conn: Any, *args: Any, **kwargs: Any) -> Any:
        read_error()
        return base.getresponse(conn, *args, **kwargs)

    async def async_getresponse(conn: Any, *args: Any, **kwargs: Any) -> Any:
        read_error()
        return await base.getresponse(conn, *args, **kwargs)

    pool.ConnectionCls = type(
        "FailingConnection",
        (base,),
        {
            "request": async_request if asynchronous else sync_request,
            "getresponse": async_getresponse if asynchronous else sync_getresponse,
        },
    )
    try:
        if failure == "write-eprototype":
            assert (await call(pool.request, "GET", "/")).status == 200
        else:
            expected = (
                ClosedPoolError
                if failure == "close-promise"
                else ReadTimeoutError
                if failure == "read-timeout"
                else ProtocolError
            )
            with pytest.raises(expected):
                await call(
                    pool.request, "GET", "/", multiplexed=failure == "close-promise"
                )
        assert observed
    finally:
        await call(pool.close)
