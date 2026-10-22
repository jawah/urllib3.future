from __future__ import annotations

import asyncio
import contextlib
import inspect
import queue
import socket
import ssl
import threading
import time
from typing import Any, Dict, Iterator, NamedTuple

import pytest
from jh2.config import H2Configuration  # type: ignore[import-untyped]
from jh2.connection import H2Connection  # type: ignore[import-untyped]
from jh2.events import DataReceived, PingReceived, RequestReceived, StreamEnded  # type: ignore[import-untyped]
from jh2.settings import SettingCodes, Settings  # type: ignore[import-untyped]

from dummyserver.server import DEFAULT_CA, DEFAULT_CERTS
from urllib3 import (
    AsyncHTTPConnectionPool,
    AsyncHTTPSConnectionPool,
    HTTPConnectionPool,
    HTTPSConnectionPool,
    HttpVersion,
)
from urllib3._async.connection import AsyncHTTPSConnection
from urllib3.connection import HTTPSConnection
from urllib3.exceptions import ProtocolError


class HTTP2Peer(NamedTuple):
    port: int
    pings: queue.Queue[bytes]
    requests: queue.Queue[Any]
    bodies: queue.Queue[bytes]


@pytest.fixture
def http2_peer(request: pytest.FixtureRequest) -> Iterator[HTTP2Peer]:
    """A TLS peer controlling flow credit, EOF, and PING acknowledgements."""
    mode = getattr(request, "param", "normal")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(DEFAULT_CERTS["certfile"], DEFAULT_CERTS["keyfile"])
    context.set_alpn_protocols([] if mode in ("no-alpn", "no-compatible") else ["h2"])
    stop = threading.Event()
    pings: queue.Queue[bytes] = queue.Queue()
    requests: queue.Queue[Any] = queue.Queue()
    bodies: queue.Queue[bytes] = queue.Queue()
    errors: list[BaseException] = []

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(0.1)

        def serve() -> None:
            try:
                while not stop.is_set():
                    try:
                        raw, _ = listener.accept()
                    except socket.timeout:
                        continue
                    with raw:
                        raw.settimeout(10)
                        transport = (
                            contextlib.nullcontext(raw)
                            if mode == "cleartext"
                            else context.wrap_socket(raw, server_side=True)
                        )
                        with transport as sock:
                            if (
                                mode == "require-http1"
                                and isinstance(sock, ssl.SSLSocket)
                                and sock.selected_alpn_protocol() is None
                            ):
                                request_bytes = b""
                                while b"\r\n\r\n" not in request_bytes:
                                    data = sock.recv(65536)
                                    if not data:
                                        break
                                    request_bytes += data
                                requests.put(request_bytes)
                                sock.sendall(
                                    b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
                                )
                                continue
                            peer = H2Connection(
                                H2Configuration(
                                    client_side=False,
                                    validate_outbound_headers=mode != "trailers",
                                )
                            )
                            uploads: dict[int, bytearray] = {}
                            tunnels: set[int] = set()
                            if mode in (
                                "upload-eof",
                                "raw-eof",
                                "flow-control",
                                "early-upload",
                            ):
                                peer.local_settings = Settings(
                                    client=False,
                                    initial_values={
                                        SettingCodes.INITIAL_WINDOW_SIZE: 1024
                                        if mode in ("flow-control", "early-upload")
                                        else 0,
                                        SettingCodes.ENABLE_CONNECT_PROTOCOL: 1,
                                    },
                                )
                            peer.initiate_connection()
                            sock.sendall(peer.data_to_send())
                            while not stop.is_set():
                                data = sock.recv(65536)
                                if not data:
                                    break
                                events = peer.receive_data(data)
                                close = False
                                for event in events:
                                    if isinstance(event, RequestReceived):
                                        requests.put(event.headers)
                                        if mode == "flow-control":
                                            if (
                                                dict(event.headers)[b":method"]
                                                == b"CONNECT"
                                            ):
                                                tunnels.add(event.stream_id)
                                                peer.send_headers(
                                                    event.stream_id,
                                                    [(b":status", b"200")],
                                                )
                                            else:
                                                uploads[event.stream_id] = bytearray()
                                        elif (
                                            mode == "require-http1"
                                            and requests.qsize() == 1
                                        ):
                                            # Reject once: a broken retry must fail the test
                                            # promptly instead of recursing indefinitely.
                                            peer.reset_stream(
                                                event.stream_id, error_code=0xD
                                            )
                                        elif mode == "early-upload":
                                            peer.send_headers(
                                                event.stream_id,
                                                [
                                                    (b":status", b"413"),
                                                    (b"content-length", b"0"),
                                                ],
                                                end_stream=True,
                                            )
                                        elif mode == "upload-eof":
                                            close = True
                                        elif mode in (
                                            "body-eof",
                                            "body-eof-partial",
                                            "raw-eof",
                                        ):
                                            headers = [(b":status", b"200")]
                                            if mode.startswith("body-eof"):
                                                headers.append(
                                                    (b"content-length", b"4")
                                                )
                                            peer.send_headers(event.stream_id, headers)
                                            if mode == "body-eof-partial":
                                                peer.send_data(event.stream_id, b"ab")
                                            close = True
                                        elif mode == "trailers":
                                            peer.send_headers(
                                                event.stream_id, [(b":status", b"200")]
                                            )
                                            peer.send_data(event.stream_id, b"ok")
                                            peer.send_headers(
                                                event.stream_id,
                                                [
                                                    (b":ignored", b"discard"),
                                                    (b"x-trailer", b"present"),
                                                ],
                                                end_stream=True,
                                            )
                                        elif mode == "goaway-body":
                                            peer.send_headers(
                                                event.stream_id, [(b":status", b"200")]
                                            )
                                            peer.send_data(
                                                event.stream_id, b"ok", end_stream=True
                                            )
                                            peer.close_connection(error_code=0)
                                            close = True
                                        else:
                                            if mode == "early":
                                                peer.send_headers(
                                                    event.stream_id,
                                                    [
                                                        (b":status", b"103"),
                                                        (
                                                            b"link",
                                                            b"</style.css>; rel=preload",
                                                        ),
                                                    ],
                                                )
                                            final_headers = [
                                                (b":status", b"200"),
                                                (b"content-length", b"0"),
                                            ]
                                            if mode == "alt-svc":
                                                final_headers.append(
                                                    (
                                                        b"alt-svc",
                                                        f'h3=":{listener.getsockname()[1]}"'.encode(),
                                                    )
                                                )
                                            peer.send_headers(
                                                event.stream_id,
                                                final_headers,
                                                end_stream=True,
                                            )
                                            if mode == "goaway-headers":
                                                peer.close_connection(error_code=0)
                                                close = True
                                            elif mode == "ping-client":
                                                peer.ping(b"server01")
                                    elif isinstance(event, PingReceived):
                                        pings.put(event.ping_data)
                                        if mode == "ignore-ping":
                                            # jh2 queues the ACK automatically; this peer drops it.
                                            peer.clear_outbound_data_buffer()
                                    elif (
                                        isinstance(event, DataReceived)
                                        and mode == "flow-control"
                                    ):
                                        peer.acknowledge_received_data(
                                            event.flow_controlled_length,
                                            event.stream_id,
                                        )
                                        peer.ping(b"credits!")
                                        if event.stream_id in tunnels:
                                            peer.send_data(
                                                event.stream_id,
                                                event.data,
                                                end_stream=event.stream_ended
                                                is not None,
                                            )
                                        else:
                                            uploads[event.stream_id].extend(event.data)
                                    elif (
                                        isinstance(event, StreamEnded)
                                        and event.stream_id in uploads
                                    ):
                                        bodies.put(bytes(uploads.pop(event.stream_id)))
                                        peer.send_headers(
                                            event.stream_id,
                                            [
                                                (b":status", b"200"),
                                                (b"content-length", b"0"),
                                            ],
                                            end_stream=True,
                                        )
                                outgoing = peer.data_to_send()
                                if outgoing:
                                    sock.sendall(outgoing)
                                if close:
                                    assert isinstance(sock, ssl.SSLSocket)
                                    # Send close_notify without waiting for the client's reply.
                                    sock.setblocking(False)
                                    with contextlib.suppress(
                                        ssl.SSLWantReadError, BlockingIOError
                                    ):
                                        try:
                                            sock.unwrap().close()
                                        except OSError as exc:
                                            # This peer deliberately closes without waiting
                                            # for in-flight HTTP/2 ACK/reset frames. OpenSSL
                                            # may observe those while finishing close_notify.
                                            # OpenSSL 1.0.2 may report peer close as errno 0.
                                            if (
                                                exc.errno != 0
                                                and getattr(exc, "reason", None)
                                                != "APPLICATION_DATA_AFTER_CLOSE_NOTIFY"
                                            ):
                                                raise
                                    break
            except BaseException as exc:
                # This client intentionally aborts immediately after TLS when
                # no enabled HTTP protocol remains. It must report RuntimeError.
                if mode != "no-compatible" or not isinstance(
                    exc, (ssl.SSLError, ConnectionResetError, BrokenPipeError)
                ):
                    errors.append(exc)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield HTTP2Peer(listener.getsockname()[1], pings, requests, bodies)
        finally:
            stop.set()
            thread.join(10)
            assert not thread.is_alive(), "HTTP/2 peer did not finish"
            assert not errors, errors


def pooled_connection(pool: Any) -> Any:
    assert pool.pool is not None
    return next(iter(pool.pool._registry.values()))


async def call(method: Any, *args: Any, **kwargs: Any) -> Any:
    result = method(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("http2_peer", ["body-eof", "body-eof-partial"], indirect=True)
@pytest.mark.parametrize("read_size", [None, 8])
async def test_http2_body_eof(
    http2_peer: HTTP2Peer, asynchronous: bool, read_size: int | None
) -> None:
    pool = (AsyncHTTPSConnectionPool if asynchronous else HTTPSConnectionPool)(
        "localhost",
        http2_peer.port,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        ca_certs=DEFAULT_CA,
        timeout=5,
        retries=False,
        disabled_svn={HttpVersion.h11, HttpVersion.h3},
        background_watch_delay=None,
    )
    try:
        response = await call(pool.request, "GET", "/", preload_content=False)
        assert response.status == 200
        with pytest.raises(ProtocolError, match="before END_STREAM"):
            await call(response.read, read_size)
    finally:
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("http2_peer", ["ignore-ping"], indirect=True)
async def test_http2_unanswered_pings_retire_connection(
    http2_peer: HTTP2Peer, asynchronous: bool
) -> None:
    pool = (AsyncHTTPSConnectionPool if asynchronous else HTTPSConnectionPool)(
        "localhost",
        http2_peer.port,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        ca_certs=DEFAULT_CA,
        timeout=5,
        retries=False,
        disabled_svn={HttpVersion.h11, HttpVersion.h3},
        background_watch_delay=None,
    )
    try:
        response = await call(pool.request, "GET", "/")
        assert response.status == 200
        assert pool.pool is not None
        conn = pooled_connection(pool)
        assert isinstance(conn, (HTTPSConnection, AsyncHTTPSConnection))
        sock = conn.sock
        for _ in range(2):
            await call(conn.ping)
            await asyncio.get_running_loop().run_in_executor(
                None, http2_peer.pings.get, True, 5
            )
        assert not conn.is_connected
        assert not conn.is_saturated
        replacement = await call(pool.request, "GET", "/")
        assert replacement.status == 200
        assert conn.sock is not sock
    finally:
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("http2_peer", ["upload-eof", "raw-eof"], indirect=True)
async def test_http2_flow_control_eof(
    http2_peer: HTTP2Peer,
    asynchronous: bool,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib3.contrib.hface.protocols.http2._h2 import HTTP2ProtocolHyperImpl
    from urllib3.contrib.webextensions.raw import RawExtensionFromHTTP
    from urllib3.contrib.webextensions._async.raw import AsyncRawExtensionFromHTTP

    eof_calls = 0
    receive = HTTP2ProtocolHyperImpl.bytes_received
    connection_lost = HTTP2ProtocolHyperImpl.connection_lost
    terminated: list[HTTP2ProtocolHyperImpl] = []

    def observe_connection_lost(protocol: HTTP2ProtocolHyperImpl) -> None:
        connection_lost(protocol)
        terminated.append(protocol)

    def observe(protocol: HTTP2ProtocolHyperImpl, data: bytes) -> None:
        nonlocal eof_calls
        if not data:
            eof_calls += 1
            # Fail promptly if the old EOF busy-loop returns; do not strand pytest.
            assert eof_calls <= 1, "Flow-control wait read EOF repeatedly"
        receive(protocol, data)

    monkeypatch.setattr(HTTP2ProtocolHyperImpl, "bytes_received", observe)
    monkeypatch.setattr(
        HTTP2ProtocolHyperImpl, "connection_lost", observe_connection_lost
    )
    pool = (AsyncHTTPSConnectionPool if asynchronous else HTTPSConnectionPool)(
        "localhost",
        http2_peer.port,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        ca_certs=DEFAULT_CA,
        timeout=5,
        retries=False,
        disabled_svn={HttpVersion.h11, HttpVersion.h3},
        background_watch_delay=None,
    )
    response = None
    try:
        if request.node.callspec.params["http2_peer"] == "raw-eof":
            extension = (
                AsyncRawExtensionFromHTTP() if asynchronous else RawExtensionFromHTTP()
            )
            response = await call(
                pool.request,
                "CONNECT",
                "/",
                headers={":protocol": "bytestream"},
                preload_content=False,
                extension=extension,
            )
            assert response.status == 200
            with pytest.raises(
                ProtocolError, match="Connection closed while waiting for flow control"
            ):
                await call(extension.send_payload, b"body")
        else:
            with pytest.raises(
                ProtocolError, match="Connection closed while waiting for flow control"
            ):
                await call(pool.request, "POST", "/", body=b"body")
        assert len(terminated) == 1
        assert not terminated[0].is_available()
    finally:
        if response is not None:
            await call(response.close)
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("http2_peer", ["cleartext", "no-alpn"], indirect=True)
async def test_http2_prior_knowledge_without_alpn(
    http2_peer: HTTP2Peer, asynchronous: bool, request: pytest.FixtureRequest
) -> None:
    cleartext = request.node.callspec.params["http2_peer"] == "cleartext"
    pool_type: Any = (
        (AsyncHTTPConnectionPool if asynchronous else HTTPConnectionPool)
        if cleartext
        else (AsyncHTTPSConnectionPool if asynchronous else HTTPSConnectionPool)
    )
    kwargs = {} if cleartext else {"ca_certs": DEFAULT_CA}
    pool = pool_type(
        "localhost",
        http2_peer.port,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        disabled_svn={HttpVersion.h11, HttpVersion.h3},
        timeout=5,
        retries=False,
        background_watch_delay=None,
        **kwargs,
    )
    try:
        response = await call(pool.request, "GET", "/")
        assert response.status == 200 and response.version == 20
        assert dict(http2_peer.requests.get(timeout=5))[b":method"] == b"GET"
    finally:
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("http2_peer", ["require-http1"], indirect=True)
async def test_http2_stream_reset_requests_http1_fallback(
    http2_peer: HTTP2Peer, asynchronous: bool
) -> None:
    pool = (AsyncHTTPSConnectionPool if asynchronous else HTTPSConnectionPool)(
        "localhost",
        http2_peer.port,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        ca_certs=DEFAULT_CA,
        disabled_svn={HttpVersion.h3},
        timeout=5,
        retries=1,
        background_watch_delay=None,
    )
    try:
        response = await call(pool.request, "GET", "/")
        assert response.status == 200 and response.version == 11
        assert isinstance(http2_peer.requests.get(timeout=5), list)
        assert http2_peer.requests.get(timeout=5).startswith(b"GET / HTTP/1.1\r\n")
    finally:
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_http2_explicit_host_replaces_authority(
    http2_peer: HTTP2Peer, asynchronous: bool
) -> None:
    pool = (AsyncHTTPSConnectionPool if asynchronous else HTTPSConnectionPool)(
        "localhost",
        http2_peer.port,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        ca_certs=DEFAULT_CA,
        disabled_svn={HttpVersion.h11, HttpVersion.h3},
        timeout=5,
        retries=False,
        background_watch_delay=None,
    )
    try:
        response = await call(
            pool.request,
            "GET",
            "/",
            headers={":authority": "discard.example", "Host": "selected.example"},
        )
        assert response.status == 200
        headers = http2_peer.requests.get(timeout=5)
        assert [v for k, v in headers if k == b":authority"] == [b"selected.example"]
    finally:
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("http2_peer", ["early"], indirect=True)
async def test_http2_blind_collection_delivers_informational_response(
    http2_peer: HTTP2Peer, asynchronous: bool
) -> None:
    pool = (AsyncHTTPSConnectionPool if asynchronous else HTTPSConnectionPool)(
        "localhost",
        http2_peer.port,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        ca_certs=DEFAULT_CA,
        disabled_svn={HttpVersion.h11, HttpVersion.h3},
        timeout=5,
        retries=False,
        background_watch_delay=None,
    )
    early = []

    async def on_early(response: Any) -> None:
        early.append(response)

    try:
        await call(
            pool.request,
            "GET",
            "/",
            multiplexed=True,
            on_early_response=on_early if asynchronous else early.append,
        )
        response = await call(pool.get_response)
        assert response.status == 200
        assert [r.status for r in early] == [103]
        assert early[0].headers["link"] == "</style.css>; rel=preload"
    finally:
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("http2_peer", ["goaway-headers", "goaway-body"], indirect=True)
async def test_http2_completed_response_on_expired_connection(
    http2_peer: HTTP2Peer, asynchronous: bool, request: pytest.FixtureRequest
) -> None:
    pool = (AsyncHTTPSConnectionPool if asynchronous else HTTPSConnectionPool)(
        "localhost",
        http2_peer.port,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        ca_certs=DEFAULT_CA,
        disabled_svn={HttpVersion.h11, HttpVersion.h3},
        timeout=5,
        retries=False,
        background_watch_delay=None,
    )
    try:
        response = await call(pool.request, "GET", "/", preload_content=False)
        conn = pooled_connection(pool)
        # A refused keepalive on an expired protocol must not interfere with
        # the response that the peer already completed.
        await call(conn.ping)
        assert await call(response.read) == (
            b"ok"
            if request.node.callspec.params["http2_peer"] == "goaway-body"
            else b""
        )
        await call(conn.close)
        assert not conn.expect_pong
        assert not await call(conn.peek_and_react)
        await call(conn.ping)
        assert conn.sock is None
    finally:
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "failure_point", ["readiness", "parse", "response", "send", "ping", "close"]
)
async def test_http2_backend_transport_failures(
    http2_peer: HTTP2Peer,
    asynchronous: bool,
    failure_point: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jh2.exceptions import H2Error  # type: ignore[import-untyped]
    from urllib3.backend import hface

    pool = (AsyncHTTPSConnectionPool if asynchronous else HTTPSConnectionPool)(
        "localhost",
        http2_peer.port,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        ca_certs=DEFAULT_CA,
        disabled_svn={HttpVersion.h11, HttpVersion.h3},
        timeout=5,
        retries=False,
        background_watch_delay=None,
    )
    injected = False
    try:
        assert (await call(pool.request, "GET", "/")).status == 200
        conn = pooled_connection(pool)
        sock, protocol = conn.sock, conn._protocol
        assert sock is not None and protocol is not None
        if failure_point in ("parse", "send"):
            await call(conn.ping)
            await asyncio.get_running_loop().run_in_executor(
                None, http2_peer.pings.get, True, 5
            )
        if failure_point == "send":
            # Keep real output queued until the peer's ACK has been read.
            protocol.ping()
        target: Any
        if failure_point == "readiness" and not asynchronous:
            target, method, expected = hface, "wait_for_read", sock
        elif failure_point in ("parse", "response"):
            target, method, expected = (
                H2Connection,
                "receive_data",
                protocol._connection,
            )
        else:
            method = {
                "readiness": "read_ready",
                "send": "sendall",
                "ping": "sendall",
                "close": "close",
            }[failure_point]
            target, expected = type(sock), sock
        original = getattr(target, method)

        def fail(current: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal injected
            if current is expected and not injected:
                injected = True
                if failure_point == "close":
                    original(current, *args, **kwargs)
                if failure_point in ("parse", "response"):
                    raise H2Error("injected parser failure")
                raise OSError("injected socket failure")
            return original(current, *args, **kwargs)

        with monkeypatch.context() as failure:
            failure.setattr(target, method, fail)
            if failure_point == "response":
                with pytest.raises(ProtocolError, match="injected parser failure"):
                    await call(pool.request, "GET", "/failure")
            elif failure_point in ("ping", "close"):
                await call(getattr(conn, failure_point))
            else:
                deadline = time.monotonic() + 5
                while not injected:
                    assert time.monotonic() < deadline, (
                        "The failing operation was not reached"
                    )
                    await call(
                        conn.peek_and_react, expect_frame=failure_point != "readiness"
                    )
        assert injected
        if failure_point != "ping":
            assert not conn.is_connected
        await call(conn.close)
        assert (await call(pool.request, "GET", "/next")).status == 200
    finally:
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("phase", ["headers", "body"])
async def test_http2_write_error_preserves_received_response(
    http2_peer: HTTP2Peer,
    asynchronous: bool,
    phase: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = (AsyncHTTPSConnectionPool if asynchronous else HTTPSConnectionPool)(
        "localhost",
        http2_peer.port,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        ca_certs=DEFAULT_CA,
        disabled_svn={HttpVersion.h11, HttpVersion.h3},
        timeout=5,
        retries=False,
        background_watch_delay=None,
    )
    injected = False
    try:
        assert (await call(pool.request, "GET", "/")).status == 200
        conn = pooled_connection(pool)
        sock = conn.sock
        assert sock is not None
        sendall = type(sock).sendall
        submit_name = "submit_headers" if phase == "headers" else "submit_data"
        submit = getattr(conn._protocol, submit_name)

        def after_control_frame(*args: Any, **kwargs: Any) -> Any:
            conn._protocol._connection.increment_flow_control_window(1)
            return submit(*args, **kwargs)

        monkeypatch.setattr(conn._protocol, submit_name, after_control_frame)

        def raise_after_frame(current: Any, data: Any) -> None:
            nonlocal injected
            # Keep the real bytes and peer response; report failure only for the
            # selected request frame, as a partial socket write may do.
            if current is sock and not injected:
                offset = 0
                while offset + 9 <= len(data):
                    if data[offset + 3] == (1 if phase == "headers" else 0):
                        injected = True
                        raise BrokenPipeError("injected partial write failure")
                    offset += 9 + int.from_bytes(data[offset : offset + 3], "big")

        def send_sync(current: Any, data: Any, *args: Any, **kwargs: Any) -> Any:
            result = sendall(current, data, *args, **kwargs)
            raise_after_frame(current, data)
            return result

        async def send_async(current: Any, data: Any, *args: Any, **kwargs: Any) -> Any:
            result = await sendall(current, data, *args, **kwargs)
            raise_after_frame(current, data)
            return result

        with monkeypatch.context() as failure:
            failure.setattr(
                type(sock), "sendall", send_async if asynchronous else send_sync
            )
            response = await call(
                pool.request,
                "GET" if phase == "headers" else "POST",
                "/",
                body=None if phase == "headers" else b"payload",
            )
        assert injected
        assert response.status == 200 and response.version == 20
    finally:
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("http2_peer", ["flow-control"], indirect=True)
@pytest.mark.parametrize("raw", [False, True])
async def test_http2_upload_processes_peer_control_frames(
    http2_peer: HTTP2Peer,
    asynchronous: bool,
    raw: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib3.contrib.webextensions.raw import RawExtensionFromHTTP
    from urllib3.contrib.webextensions._async.raw import AsyncRawExtensionFromHTTP

    pool = (AsyncHTTPSConnectionPool if asynchronous else HTTPSConnectionPool)(
        "localhost",
        http2_peer.port,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        ca_certs=DEFAULT_CA,
        disabled_svn={HttpVersion.h11, HttpVersion.h3},
        timeout=5,
        retries=False,
        background_watch_delay=None,
    )
    response = None
    try:
        assert (await call(pool.request, "GET", "/")).status == 200
        assert http2_peer.bodies.get(timeout=5) == b""
        conn = pooled_connection(pool)
        waiting = conn._protocol.should_wait_remote_flow_control
        waits = 0

        def observe(stream_id: int, size: int) -> Any:
            nonlocal waits
            result = waiting(stream_id, size)
            waits += result is True
            return result

        monkeypatch.setattr(conn._protocol, "should_wait_remote_flow_control", observe)
        body = b"x" * 16384
        if raw:
            extension = (
                AsyncRawExtensionFromHTTP() if asynchronous else RawExtensionFromHTTP()
            )
            response = await call(
                pool.request,
                "CONNECT",
                "/",
                preload_content=False,
                headers={":protocol": "bytestream"},
                extension=extension,
            )
            for offset in range(0, len(body), 512):
                await call(extension.send_payload, body[offset : offset + 512])
            received = bytearray()
            while len(received) < len(body):
                chunk = await call(extension.next_payload)
                assert chunk
                received.extend(chunk)
            assert received == body
            await call(extension.close)
            await call(conn.ping)
            deadline = time.monotonic() + 5
            while conn.expect_pong:
                assert time.monotonic() < deadline
                await call(conn.peek_and_react, expect_frame=True)
        else:
            assert (await call(pool.request, "POST", "/", body=body)).status == 200
            assert http2_peer.bodies.get(timeout=5) == body
        assert waits > 0
    finally:
        if response is not None:
            await call(response.close)
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("http2_peer", ["body-eof"], indirect=True)
async def test_http2_peek_observes_tcp_eof(
    http2_peer: HTTP2Peer, asynchronous: bool
) -> None:
    pool = (AsyncHTTPSConnectionPool if asynchronous else HTTPSConnectionPool)(
        "localhost",
        http2_peer.port,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        ca_certs=DEFAULT_CA,
        disabled_svn={HttpVersion.h11, HttpVersion.h3},
        timeout=5,
        retries=False,
        background_watch_delay=None,
    )
    response = None
    try:
        response = await call(pool.request, "GET", "/", preload_content=False)
        conn = pooled_connection(pool)
        protocol = conn._protocol
        deadline = time.monotonic() + 5
        while not protocol._terminated:
            assert time.monotonic() < deadline, "EOF did not retire the protocol"
            await call(conn.peek_and_react, expect_frame=True)
        await call(conn.ping)
        with pytest.raises(ProtocolError):
            await call(response.read)
    finally:
        if response is not None:
            await call(response.close)
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("http2_peer", ["alt-svc"], indirect=True)
async def test_upgrade_cache_can_decline_an_advertised_alternative(
    http2_peer: HTTP2Peer, asynchronous: bool, requires_http3: None
) -> None:
    class DecliningCache(Dict[Any, Any]):
        writes = 0

        def __setitem__(self, key: Any, value: Any) -> None:
            self.writes += 1

    cache = DecliningCache()
    pool = (AsyncHTTPSConnectionPool if asynchronous else HTTPSConnectionPool)(
        "localhost",
        http2_peer.port,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        ca_certs=DEFAULT_CA,
        preemptive_quic_cache=cache,
        timeout=5,
        retries=False,
        background_watch_delay=None,
    )
    try:
        response = await call(pool.request, "GET", "/")
        assert response.status == 200 and response.version == 20
        conn = pooled_connection(pool)
        sock = conn.sock
        assert cache.writes == 1 and not cache
        assert (await call(pool.request, "GET", "/next")).version == 20
        assert conn.sock is sock
    finally:
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("http2_peer", ["no-compatible"], indirect=True)
async def test_negative_quic_cache_with_only_quic_enabled_reports_incompatibility(
    http2_peer: HTTP2Peer, asynchronous: bool, requires_http3: None
) -> None:
    pool = (AsyncHTTPSConnectionPool if asynchronous else HTTPSConnectionPool)(
        "localhost",
        http2_peer.port,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        ca_certs=DEFAULT_CA,
        disabled_svn={HttpVersion.h11, HttpVersion.h2},
        preemptive_quic_cache={("localhost", http2_peer.port): None},
        timeout=5,
        retries=False,
        background_watch_delay=None,
    )
    try:
        with pytest.raises(RuntimeError, match="No compatible protocol"):
            await call(pool.request, "GET", "/")
    finally:
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("http2_peer", ["trailers"], indirect=True)
async def test_http2_response_discards_pseudo_headers_from_trailers(
    http2_peer: HTTP2Peer, asynchronous: bool
) -> None:
    pool = (AsyncHTTPSConnectionPool if asynchronous else HTTPSConnectionPool)(
        "localhost",
        http2_peer.port,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        ca_certs=DEFAULT_CA,
        disabled_svn={HttpVersion.h11, HttpVersion.h3},
        timeout=5,
        retries=False,
        background_watch_delay=None,
    )
    try:
        response = await call(pool.request, "GET", "/", preload_content=False)
        assert await call(response.read, 8) == b"ok"
        assert response.trailers == {"x-trailer": "present"}
    finally:
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("http2_peer", ["early-upload"], indirect=True)
async def test_upload_callback_reports_an_early_response(
    http2_peer: HTTP2Peer, asynchronous: bool
) -> None:
    pool = (AsyncHTTPSConnectionPool if asynchronous else HTTPSConnectionPool)(
        "localhost",
        http2_peer.port,
        ca_certs=DEFAULT_CA,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        disabled_svn={HttpVersion.h11, HttpVersion.h3},
        timeout=5,
        retries=False,
        background_watch_delay=None,
    )
    progress: list[tuple[Any, ...]] = []

    def record(*args: Any) -> None:
        progress.append(args)

    async def record_async(*args: Any) -> None:
        record(*args)

    try:
        response = await call(
            pool.request,
            "POST",
            "/",
            body=b"x" * 4096,
            on_upload_body=record_async if asynchronous else record,
        )
        assert response.status == 413
        sent, total, completed, interrupted = progress[-1]
        assert 0 < sent < total == 4096
        assert not completed and interrupted
    finally:
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("http2_peer", ["cleartext", "normal"], indirect=True)
async def test_live_promise_and_connection_metadata(
    http2_peer: HTTP2Peer, asynchronous: bool, request: pytest.FixtureRequest
) -> None:
    cleartext = request.node.callspec.params["http2_peer"] == "cleartext"
    pool_type: Any = (
        (AsyncHTTPConnectionPool if asynchronous else HTTPConnectionPool)
        if cleartext
        else (AsyncHTTPSConnectionPool if asynchronous else HTTPSConnectionPool)
    )
    pool = pool_type(
        "localhost",
        http2_peer.port,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        disabled_svn={HttpVersion.h11, HttpVersion.h3},
        timeout=5,
        retries=False,
        background_watch_delay=None,
        **({} if cleartext else {"ca_certs": DEFAULT_CA}),
    )
    try:
        promise = await call(
            pool.request, "GET", "/", multiplexed=True, preload_content=False
        )
        conn = pooled_connection(pool)
        assert promise in conn and not promise.is_ready
        assert promise != object()
        assert "HTTP/2.0" in repr(promise)
        assert conn.conn_info.is_encrypted() is not cleartext
        assert "http_version" in repr(conn.conn_info)
        with pytest.raises(OSError):
            _ = promise.response
        response = await call(pool.get_response, promise=promise)
        assert promise.is_ready and promise not in conn
        assert promise.response is response._fp
        assert promise.response.method == "GET"
        await call(response.read)
    finally:
        await call(pool.close)
