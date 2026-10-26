from __future__ import annotations

import errno
import socket
import sys
import time
import typing
from pathlib import Path
from typing import Any, Callable, Dict, Iterator

import pytest
from jh2.connection import H2Connection  # type: ignore[import-untyped]

from dummyserver.server import DEFAULT_CA
from test import notWindows
from test.with_dummyserver.conftest import HTTP2Peer, HTTP3Peer
from urllib3 import (
    HTTPConnectionPool,
    HTTPSConnectionPool,
    HttpVersion,
)
from urllib3.connection import HTTPSConnection
from urllib3.contrib.webextensions.raw import RawExtensionFromHTTP
from urllib3.exceptions import ProtocolError
from urllib3.util.wait import wait_for_read


def pooled_connection(pool: Any) -> Any:
    assert pool.pool is not None
    return next(iter(pool.pool._registry.values()))


@pytest.mark.filterwarnings(
    "ignore:Unverified HTTPS request:urllib3.exceptions.InsecureRequestWarning"
)
@pytest.mark.parametrize(
    "mode",
    [
        "directory",
        "search-path",
        "bundle",
        "unverified",
        "unhashed",
        "explicit-file",
        "explicit-data",
        "explicit-directory",
        "explicit-context",
    ],
)
def test_http3_default_ca_directory(
    http3_peer: HTTP3Peer,
    default_ca_directory: Callable[[str, str], dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    from urllib3.exceptions import SSLError

    options = default_ca_directory(http3_peer.ca_data, mode)
    if mode in ("bundle", "unverified") or mode.startswith("explicit-"):

        def unexpected_scan(*args: Any) -> None:
            pytest.fail("Default CA directory scanned outside the empty-store fallback")

        monkeypatch.setattr(
            "urllib3.backend.hface._load_default_certs_eagerly", unexpected_scan
        )

    with HTTPSConnectionPool(
        "localhost",
        http3_peer.port,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        disabled_svn={HttpVersion.h11, HttpVersion.h2},
        timeout=5,
        retries=False,
        background_watch_delay=None,
        **options,
    ) as pool:
        if mode == "unhashed" or mode.startswith("explicit-"):
            with pytest.raises(SSLError):
                pool.request("GET", "/")
        else:
            response = pool.request("GET", "/")
            assert response.status == 200
            assert response.version == 30


@pytest.mark.parametrize("http2_peer", ["body-eof", "body-eof-partial"], indirect=True)
@pytest.mark.parametrize("read_size", [None, 8])
def test_http2_body_eof(http2_peer: HTTP2Peer, read_size: int | None) -> None:
    pool = HTTPSConnectionPool(
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
        response = pool.request("GET", "/", preload_content=False)
        assert response.status == 200
        with pytest.raises(ProtocolError, match="before END_STREAM"):
            response.read(read_size)
    finally:
        pool.close()


@pytest.mark.parametrize("http2_peer", ["ignore-ping"], indirect=True)
def test_http2_unanswered_pings_retire_connection(
    http2_peer: HTTP2Peer,
) -> None:
    pool = HTTPSConnectionPool(
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
        response = pool.request("GET", "/")
        assert response.status == 200
        assert pool.pool is not None
        conn = pooled_connection(pool)
        assert isinstance(conn, HTTPSConnection)
        sock = conn.sock
        for _ in range(2):
            conn.ping()
            http2_peer.pings.get(True, 5)
        assert not conn.is_connected
        assert not conn.is_saturated
        replacement = pool.request("GET", "/")
        assert replacement.status == 200
        assert conn.sock is not sock
    finally:
        pool.close()


@pytest.mark.parametrize("http2_peer", ["upload-eof", "raw-eof"], indirect=True)
def test_http2_flow_control_eof(
    http2_peer: HTTP2Peer,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib3.contrib.hface.protocols.http2._h2 import HTTP2ProtocolHyperImpl
    from urllib3.contrib.webextensions.raw import RawExtensionFromHTTP

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
    pool = HTTPSConnectionPool(
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
            extension = RawExtensionFromHTTP()
            response = pool.request(
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
                extension.send_payload(b"body")
        else:
            with pytest.raises(
                ProtocolError, match="Connection closed while waiting for flow control"
            ):
                pool.request("POST", "/", body=b"body")
        assert len(terminated) == 1
        assert not terminated[0].is_available()
    finally:
        if response is not None:
            response.close()
        pool.close()


@pytest.mark.parametrize("http2_peer", ["cleartext", "no-alpn"], indirect=True)
def test_http2_prior_knowledge_without_alpn(
    http2_peer: HTTP2Peer, request: pytest.FixtureRequest
) -> None:
    cleartext = request.node.callspec.params["http2_peer"] == "cleartext"
    pool_type: Any = HTTPConnectionPool if cleartext else HTTPSConnectionPool
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
        response = pool.request("GET", "/")
        assert response.status == 200 and response.version == 20
        assert dict(http2_peer.requests.get(timeout=5))[b":method"] == b"GET"
    finally:
        pool.close()


@pytest.mark.parametrize("http2_peer", ["require-http1"], indirect=True)
def test_http2_stream_reset_requests_http1_fallback(
    http2_peer: HTTP2Peer,
) -> None:
    pool = HTTPSConnectionPool(
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
        response = pool.request("GET", "/")
        assert response.status == 200 and response.version == 11
        assert isinstance(http2_peer.requests.get(timeout=5), list)
        assert http2_peer.requests.get(timeout=5).startswith(b"GET / HTTP/1.1\r\n")
    finally:
        pool.close()


def test_http2_explicit_host_replaces_authority(
    http2_peer: HTTP2Peer,
) -> None:
    pool = HTTPSConnectionPool(
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
        response = pool.request(
            "GET",
            "/",
            headers={":authority": "discard.example", "Host": "selected.example"},
        )
        assert response.status == 200
        headers = http2_peer.requests.get(timeout=5)
        assert [v for k, v in headers if k == b":authority"] == [b"selected.example"]
    finally:
        pool.close()


@pytest.mark.parametrize("http2_peer", ["early"], indirect=True)
def test_http2_blind_collection_delivers_informational_response(
    http2_peer: HTTP2Peer,
) -> None:
    pool = HTTPSConnectionPool(
        "localhost",
        http2_peer.port,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        ca_certs=DEFAULT_CA,
        disabled_svn={HttpVersion.h11, HttpVersion.h3},
        timeout=5,
        retries=False,
        background_watch_delay=None,
    )
    early: list[Any] = []

    try:
        pool.request(
            "GET",
            "/",
            multiplexed=True,
            on_early_response=early.append,
        )
        response = pool.get_response()
        assert response is not None
        assert response.status == 200
        assert [r.status for r in early] == [103]
        assert early[0].headers["link"] == "</style.css>; rel=preload"
    finally:
        pool.close()


@pytest.mark.parametrize("http2_peer", ["goaway-headers", "goaway-body"], indirect=True)
def test_http2_completed_response_on_expired_connection(
    http2_peer: HTTP2Peer, request: pytest.FixtureRequest
) -> None:
    pool = HTTPSConnectionPool(
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
        response = pool.request("GET", "/", preload_content=False)
        conn = pooled_connection(pool)
        # A refused keepalive on an expired protocol must not interfere with
        # the response that the peer already completed.
        conn.ping()
        assert response.read() == (
            b"ok"
            if request.node.callspec.params["http2_peer"] == "goaway-body"
            else b""
        )
        conn.close()
        assert not conn.expect_pong
        assert not conn.peek_and_react()
        conn.ping()
        assert conn.sock is None
    finally:
        pool.close()


@pytest.mark.parametrize(
    "failure_point", ["readiness", "parse", "response", "send", "ping", "close"]
)
def test_http2_backend_transport_failures(
    http2_peer: HTTP2Peer,
    failure_point: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jh2.exceptions import H2Error  # type: ignore[import-untyped]

    from urllib3.backend import hface

    pool = HTTPSConnectionPool(
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
        assert pool.request("GET", "/").status == 200
        conn = pooled_connection(pool)
        sock, protocol = conn.sock, conn._protocol
        assert sock is not None and protocol is not None
        if failure_point in ("parse", "send"):
            conn.ping()
            http2_peer.pings.get(True, 5)
        if failure_point == "send":
            # Keep real output queued until the peer's ACK has been read.
            protocol.ping()
        target: Any
        if failure_point == "readiness":
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
                    pool.request("GET", "/failure")
            elif failure_point in ("ping", "close"):
                getattr(conn, failure_point)()
            else:
                deadline = time.monotonic() + 5
                while not injected:
                    assert time.monotonic() < deadline, (
                        "The failing operation was not reached"
                    )
                    conn.peek_and_react(expect_frame=failure_point != "readiness")
        assert injected
        if failure_point != "ping":
            assert not conn.is_connected
        conn.close()
        assert pool.request("GET", "/next").status == 200
    finally:
        pool.close()


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_http2_write_error_preserves_received_response(
    http2_peer: HTTP2Peer,
    phase: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = HTTPSConnectionPool(
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
        assert pool.request("GET", "/").status == 200
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

        with monkeypatch.context() as failure:
            failure.setattr(type(sock), "sendall", send_sync)
            response = pool.request(
                "GET" if phase == "headers" else "POST",
                "/",
                body=None if phase == "headers" else b"payload",
            )
        assert injected
        assert response.status == 200 and response.version == 20
    finally:
        pool.close()


@pytest.mark.parametrize("http2_peer", ["flow-control"], indirect=True)
@pytest.mark.parametrize("raw", [False, True])
def test_http2_upload_processes_peer_control_frames(
    http2_peer: HTTP2Peer,
    raw: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib3.contrib.webextensions.raw import RawExtensionFromHTTP

    pool = HTTPSConnectionPool(
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
        assert pool.request("GET", "/").status == 200
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
            extension = RawExtensionFromHTTP()
            response = pool.request(
                "CONNECT",
                "/",
                preload_content=False,
                headers={":protocol": "bytestream"},
                extension=extension,
            )
            for offset in range(0, len(body), 512):
                extension.send_payload(body[offset : offset + 512])
            received = bytearray()
            while len(received) < len(body):
                chunk = extension.next_payload()
                assert chunk
                received.extend(chunk)
            assert received == body
            extension.close()
            conn.ping()
            deadline = time.monotonic() + 5
            while conn.expect_pong:
                assert time.monotonic() < deadline
                conn.peek_and_react(expect_frame=True)
        else:
            assert pool.request("POST", "/", body=body).status == 200
            assert http2_peer.bodies.get(timeout=5) == body
        assert waits > 0
    finally:
        if response is not None:
            response.close()
        pool.close()


@pytest.mark.parametrize("http2_peer", ["body-eof"], indirect=True)
def test_http2_peek_observes_tcp_eof(
    http2_peer: HTTP2Peer,
) -> None:
    pool = HTTPSConnectionPool(
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
        response = pool.request("GET", "/", preload_content=False)
        conn = pooled_connection(pool)
        protocol = conn._protocol
        deadline = time.monotonic() + 5
        while not protocol._terminated:
            assert time.monotonic() < deadline, "EOF did not retire the protocol"
            conn.peek_and_react(expect_frame=True)
        conn.ping()
        with pytest.raises(ProtocolError):
            response.read()
    finally:
        if response is not None:
            response.close()
        pool.close()


@pytest.mark.parametrize("http2_peer", ["alt-svc"], indirect=True)
def test_upgrade_cache_can_decline_an_advertised_alternative(
    http2_peer: HTTP2Peer, requires_http3: None
) -> None:
    class DecliningCache(Dict[Any, Any]):
        writes = 0

        def __setitem__(self, key: Any, value: Any) -> None:
            self.writes += 1

    cache = DecliningCache()
    pool = HTTPSConnectionPool(
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
        response = pool.request("GET", "/")
        assert response.status == 200 and response.version == 20
        conn = pooled_connection(pool)
        sock = conn.sock
        assert cache.writes == 1 and not cache
        assert pool.request("GET", "/next").version == 20
        assert conn.sock is sock
    finally:
        pool.close()


@pytest.mark.parametrize("http2_peer", ["no-compatible"], indirect=True)
def test_negative_quic_cache_with_only_quic_enabled_reports_incompatibility(
    http2_peer: HTTP2Peer, requires_http3: None
) -> None:
    pool = HTTPSConnectionPool(
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
            pool.request("GET", "/")
    finally:
        pool.close()


@pytest.mark.parametrize("http2_peer", ["trailers"], indirect=True)
def test_http2_response_discards_pseudo_headers_from_trailers(
    http2_peer: HTTP2Peer,
) -> None:
    pool = HTTPSConnectionPool(
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
        response = pool.request("GET", "/", preload_content=False)
        assert response.read(8) == b"ok"
        assert response.trailers == {"x-trailer": "present"}
    finally:
        pool.close()


@pytest.mark.parametrize("http2_peer", ["early-upload"], indirect=True)
def test_upload_callback_reports_an_early_response(
    http2_peer: HTTP2Peer,
) -> None:
    pool = HTTPSConnectionPool(
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

    try:
        response = pool.request(
            "POST",
            "/",
            body=b"x" * 4096,
            on_upload_body=record,
        )
        assert response.status == 413
        sent, total, completed, interrupted = progress[-1]
        assert 0 < sent < total == 4096
        assert not completed and interrupted
    finally:
        pool.close()


@pytest.mark.parametrize("http2_peer", ["cleartext", "normal"], indirect=True)
def test_live_promise_and_connection_metadata(
    http2_peer: HTTP2Peer, request: pytest.FixtureRequest
) -> None:
    cleartext = request.node.callspec.params["http2_peer"] == "cleartext"
    pool_type: Any = HTTPConnectionPool if cleartext else HTTPSConnectionPool
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
        promise = pool.request("GET", "/", multiplexed=True, preload_content=False)
        conn = pooled_connection(pool)
        assert promise in conn and not promise.is_ready
        assert promise != object()
        assert "HTTP/2.0" in repr(promise)
        assert conn.conn_info.is_encrypted() is not cleartext
        assert "http_version" in repr(conn.conn_info)
        with pytest.raises(OSError):
            _ = promise.response
        response = pool.get_response(promise=promise)
        assert promise.is_ready and promise not in conn
        assert promise.response is response._fp
        assert promise.response.method == "GET"
        response.read()
    finally:
        pool.close()


@pytest.mark.parametrize("http2_peer", ["flow-control", "raw-eof"], indirect=True)
def test_raw_close_survives_failed_fin_write(
    http2_peer: HTTP2Peer,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib3.contrib.webextensions.raw import RawExtensionFromHTTP

    peer_closed = request.node.callspec.params["http2_peer"] == "raw-eof"
    pool = HTTPSConnectionPool(
        "localhost",
        http2_peer.port,
        ca_certs=DEFAULT_CA,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        disabled_svn={HttpVersion.h11, HttpVersion.h3},
        timeout=5,
        retries=False,
        background_watch_delay=None,
    )
    extension = RawExtensionFromHTTP()
    assert isinstance(extension, RawExtensionFromHTTP)
    close = extension.close
    closing = False

    def bounded_close() -> None:
        nonlocal closing
        assert not closing, "Raw cleanup recursively entered close()"
        closing = True
        try:
            close()
        finally:
            closing = False

    monkeypatch.setattr(extension, "close", bounded_close)
    conn = None
    try:
        response = pool.request(
            "CONNECT",
            "/",
            extension=extension,
            headers={":protocol": "bytestream"},
            preload_content=False,
        )
        conn = pooled_connection(pool)
        sock = conn.sock
        stream_id = typing.cast("Any", response._fp).from_promise.stream_id
        assert sock is not None
        if not peer_closed:
            extension.send_payload(b"hello")
            assert extension.next_payload() == b"hello"
        sendall = type(sock).sendall
        rejected = False

        def reject_fin(current: Any, data: Any) -> None:
            nonlocal rejected
            if current is not sock or rejected:
                return
            offset = 0
            while offset + 9 <= len(data):
                length = int.from_bytes(data[offset : offset + 3], "big")
                if (
                    data[offset + 3] == 0
                    and data[offset + 4] & 1
                    and int.from_bytes(data[offset + 5 : offset + 9], "big")
                    == stream_id
                ):
                    # jh2 already marked the sending side closed when it encoded FIN.
                    # Model the TLS writer refusing the encoded bytes afterward.
                    rejected = True
                    raise ConnectionResetError("injected FIN write failure")
                offset += 9 + length

        def send_sync(current: Any, data: Any, *args: Any, **kwargs: Any) -> Any:
            reject_fin(current, data)
            return sendall(current, data, *args, **kwargs)

        with monkeypatch.context() as failure:
            failure.setattr(type(sock), "sendall", send_sync)
            if peer_closed:
                with pytest.raises(
                    ProtocolError,
                    match="Connection closed while waiting for flow control",
                ):
                    extension.send_payload(b"body")
            else:
                extension.close()
        assert rejected
        assert extension.closed and response.closed
        extension.close()
        response.close()
        if peer_closed:
            assert conn.is_closed
        else:
            assert pool.request("GET", "/next").status == 200
            assert pool.num_connections == 1
    finally:
        if conn is not None:
            conn.close()
        pool.close()


def pool_for(peer: HTTP3Peer) -> Any:
    return HTTPSConnectionPool(
        "localhost",
        peer.port,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        ca_cert_data=peer.ca_data,
        timeout=5,
        retries=False,
        disabled_svn={HttpVersion.h11, HttpVersion.h2},
        background_watch_delay=None,
    )


def observe(conn: Any, ready: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while not ready():
        assert time.monotonic() < deadline, "Peer event did not reach the client"
        conn.peek_and_react(expect_frame=True)


@notWindows()
@pytest.mark.parametrize("retirement", ["unanswered-pings", "goaway", "idle-timeout"])
def test_http3_connection_retirement(
    http3_peer: HTTP3Peer,
    retirement: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = pool_for(http3_peer)
    try:
        assert pool.request("GET", "/").status == 200
        conn = next(iter(pool.pool._registry.values()))
        assert isinstance(conn, HTTPSConnection)
        sock, protocol = conn.sock, conn._protocol
        assert protocol is not None
        if retirement == "unanswered-pings":
            http3_peer.act(lambda peer: setattr(peer, "blackhole", True))
            conn.ping()
            conn.ping()
        elif retirement == "goaway":
            from qh3._hazmat import encode_uint_var
            from qh3.h3.connection import FrameType, encode_frame

            http3_peer.act(
                lambda peer: peer._quic.send_stream_data(
                    peer.http._local_control_stream_id,
                    encode_frame(FrameType.GOAWAY, encode_uint_var(4)),
                )
            )
            observe(conn, lambda: bool(getattr(protocol, "_goaway_to_honor")))
        else:
            from urllib3.contrib.hface.protocols.http3 import _qh3

            # Advance the clock supplied to the native QUIC timer after a real handshake.
            expired = time.time() + 600
            with monkeypatch.context() as clock:
                clock.setattr(_qh3, "monotonic", lambda: expired)
                assert not conn.is_connected
        assert not conn.is_connected
        assert not conn.is_saturated
        assert pool.request("GET", "/next").status == 200
        assert conn.sock is not sock
    finally:
        pool.close()


@notWindows()
def test_http3_receive_error_retires_connection(
    http3_peer: HTTP3Peer, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = pool_for(http3_peer)
    try:
        assert pool.request("GET", "/").status == 200
        conn = next(iter(pool.pool._registry.values()))
        sock = conn.sock
        assert sock is not None
        receive_method = "recvmsg" if conn._dgram_gro_enabled else "recv"
        receive = getattr(type(sock), receive_method)

        def fail_receive(current: Any, *args: Any, **kwargs: Any) -> Any:
            if current is sock:
                raise ConnectionResetError("injected receive failure")
            return receive(current, *args, **kwargs)

        # Inject only the OS receive failure after establishing the real QUIC connection.
        with monkeypatch.context() as failure:
            failure.setattr(type(sock), receive_method, fail_receive)
            assert conn.peek_and_react(expect_frame=True)
        assert not conn.is_connected
        assert pool.request("GET", "/next").status == 200
        assert conn.sock is not sock
    finally:
        pool.close()


def test_http3_empty_raw_response_closes_after_goaway(
    http3_peer: HTTP3Peer,
) -> None:
    from qh3._hazmat import encode_uint_var
    from qh3.h3.connection import FrameType, encode_frame

    from urllib3.contrib.webextensions.raw import RawExtensionFromHTTP

    pool = pool_for(http3_peer)
    response = None
    try:
        extension = RawExtensionFromHTTP()
        response = pool.request(
            "CONNECT",
            "/empty-tunnel",
            headers={":protocol": "bytestream"},
            preload_content=False,
            extension=extension,
        )
        conn = next(iter(pool.pool._registry.values()))
        protocol = conn._protocol
        assert protocol is not None and response.status == 200
        http3_peer.act(
            lambda peer: peer._quic.send_stream_data(
                peer.http._local_control_stream_id,
                encode_frame(FrameType.GOAWAY, encode_uint_var(4)),
            )
        )
        observe(conn, lambda: bool(getattr(protocol, "_goaway_to_honor")))
        extension.close()
        assert conn.sock is None
    finally:
        if response is not None:
            response.close()
        pool.close()


def test_http3_response_close_after_explicit_connection_close(
    http3_peer: HTTP3Peer,
) -> None:
    pool = pool_for(http3_peer)
    response = None
    try:
        response = pool.request("GET", "/partial-response", preload_content=False)
        conn = next(iter(pool.pool._registry.values()))
        conn.close()
        response.close()
        assert conn.sock is None and response.closed
    finally:
        if response is not None:
            response.close()
        pool.close()


def test_http3_batch_engine_error_is_translated(
    http3_peer: HTTP3Peer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qh3 import QuicConnectionError

    from urllib3.backend import hface
    from urllib3.exceptions import ProtocolError

    pool = pool_for(http3_peer)
    injected = False
    try:
        promise = pool.request("GET", "/", multiplexed=True)
        conn = next(iter(pool.pool._registry.values()))
        sock, protocol = conn.sock, conn._protocol
        assert sock is not None and protocol is not None
        target: Any
        if conn._dgram_gro_enabled:
            target, name = hface, "sync_recv_gro"
        else:
            target, name = type(sock), "recv"
        receive = getattr(target, name)

        def received_batch(current: Any, *args: Any, **kwargs: Any) -> Any:
            result = receive(current, *args, **kwargs)
            return (
                result
                if current is not sock or not result or isinstance(result, list)
                else [result]
            )

        def engine_failure(*args: Any, **kwargs: Any) -> None:
            nonlocal injected
            injected = True
            raise QuicConnectionError(
                error_code=1,
                frame_type=None,
                reason_phrase="injected QUIC engine failure",
            )

        with monkeypatch.context() as failure:
            failure.setattr(target, name, received_batch)
            failure.setattr(protocol._quic, "receive_many_datagrams", engine_failure)
            with pytest.raises(ProtocolError, match="injected QUIC engine failure"):
                pool.get_response(promise=promise)
        assert injected
        # The injected error did not alter the QUIC state. A caller can resume
        # collection when the real peer retransmits the unacknowledged packet.
        assert pool.get_response(promise=promise).status == 200
        assert pool.request("GET", "/next").status == 200
    finally:
        pool.close()


@pytest.mark.parametrize("http2_peer", ["require-http1"], indirect=True)
def test_http3_then_http2_fallback_replaces_rejected_connections(
    http3_peer: HTTP3Peer,
    http2_peer: HTTP2Peer,
) -> None:
    pool = HTTPSConnectionPool(
        "localhost",
        http2_peer.port,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        ca_cert_data=http3_peer.ca_data + Path(DEFAULT_CA).read_text(),
        preemptive_quic_cache={
            ("localhost", http2_peer.port): ("localhost", http3_peer.port)
        },
        timeout=5,
        retries=1,
        background_watch_delay=None,
    )
    try:
        response = pool.request("GET", "/fallback")
        assert response.status == 200 and response.version == 11
        assert pool.num_connections == 3
        assert isinstance(http2_peer.requests.get(timeout=5), list)
        assert http2_peer.requests.get(timeout=5).startswith(
            b"GET /fallback HTTP/1.1\r\n"
        )
    finally:
        pool.close()


def test_raw_stream_bounded_reads_preserve_buffer_and_eof(
    http3_peer: HTTP3Peer,
) -> None:
    from urllib3.contrib.webextensions.raw import RawExtensionFromHTTP

    pool = pool_for(http3_peer)
    response = None
    try:
        extension = RawExtensionFromHTTP()
        response = pool.request(
            "CONNECT",
            "/",
            headers={":protocol": "bytestream"},
            preload_content=False,
            extension=extension,
        )
        dsa = extension._dsa
        assert dsa is not None and not dsa.closed
        stream_id = typing.cast("Any", response._fp).from_promise.stream_id
        http3_peer.act(
            lambda peer: peer.http.send_data(stream_id, b"abcdef", end_stream=False)
        )

        # Exercise the real low-level socket-compatible API. No read callback
        # or receive buffer is fabricated; the bytes came from the QUIC peer.
        def read_expected(expected: bytes, size: int | None) -> None:
            data = bytearray()
            while len(data) < len(expected):
                amount = None if size is None else min(size, len(expected) - len(data))
                chunk, ended, _ = dsa.recv_extended(amount)
                assert chunk and not ended
                data.extend(chunk)
            assert data == expected

        read_expected(b"ab", 2)
        read_expected(b"cd", 2)
        read_expected(b"ef", None)
        http3_peer.act(
            lambda peer: peer.http.send_data(stream_id, b"", end_stream=True)
        )
        data, ended, _ = dsa.recv_extended(2)
        assert data == b"" and ended
        with pytest.raises(OSError, match="closed"):
            dsa.recv_extended(1)
        extension.close()
        assert dsa.closed
        with pytest.raises(OSError, match="write not permitted"):
            dsa.sendall(b"late")
    finally:
        if response is not None:
            response.close()
        pool.close()


@pytest.mark.parametrize("peer_stops_upload", [False, True])
def test_http3_close_raw_stream_preserves_connection(
    http3_peer: HTTP3Peer,
    peer_stops_upload: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib3.contrib.webextensions.raw import RawExtensionFromHTTP

    pool = pool_for(http3_peer)
    response = None
    try:
        extension = RawExtensionFromHTTP()
        assert isinstance(extension, RawExtensionFromHTTP)
        close = extension.close
        closing = False

        def bounded_close() -> None:
            nonlocal closing
            # Fail on recursive cleanup instead of stranding a pytest worker.
            assert not closing, "Raw extension cleanup re-entered close()"
            closing = True
            try:
                close()
            finally:
                closing = False

        monkeypatch.setattr(extension, "close", bounded_close)
        response = pool.request(
            "CONNECT",
            "/",
            headers={":protocol": "bytestream"},
            extension=extension,
            preload_content=False,
        )
        assert response.status == 200
        conn = response.connection
        assert conn is not None and conn._protocol is not None
        protocol = conn._protocol
        stream_id = typing.cast("Any", response._fp).from_promise.stream_id
        if peer_stops_upload:
            http3_peer.act(lambda peer: peer._quic.stop_stream(stream_id, 0x10C))
            observe(conn, lambda: stream_id not in protocol._sending_streams)
        response.close()
        response = None
        assert not protocol._open_streams
        assert not protocol._sending_streams
        assert pool.request("GET", "/next").status == 200
        assert pool.num_connections == 1
    finally:
        if response is not None:
            response.close()
        pool.close()


def test_http3_trailers_before_separate_fin(http3_peer: HTTP3Peer) -> None:
    pool = pool_for(http3_peer)
    response = None
    try:
        response = pool.request("GET", "/partial-response", preload_content=False)
        assert response.read(2) == b"ab"
        conn = response.connection
        assert conn is not None and conn._protocol is not None
        protocol = conn._protocol
        stream_id = typing.cast("Any", response._fp).from_promise.stream_id

        def send_trailers(peer: Any) -> None:
            peer.http.send_data(stream_id, b"cd", end_stream=False)
            peer.http.send_headers(stream_id, [(b"x-trailer", b"present")])

        http3_peer.act(send_trailers)
        # Decode trailers before sending FIN so qh3 emits a separate empty DATA
        # event, independently of how the OS batches the received datagrams.
        observe(
            conn,
            lambda: (
                protocol._http._stream[stream_id].headers_recv_state.name
                == "AFTER_TRAILERS"
            ),
        )
        http3_peer.act(
            lambda peer: peer._quic.send_stream_data(stream_id, b"", end_stream=True)
        )
        assert response.read() == b"cd"
        assert response.trailers == {"x-trailer": "present"}
        assert pool.request("GET", "/next").status == 200
        assert pool.num_connections == 1
    finally:
        if response is not None:
            response.close()
        pool.close()


@pytest.mark.parametrize("http3_peer", ["early-reject"], indirect=True)
def test_http3_stop_sending_after_response_ended(http3_peer: HTTP3Peer) -> None:
    from qh3 import quic_events

    if not hasattr(quic_events, "StopSendingReceived"):
        pytest.skip("qh3 does not expose STOP_SENDING events")
    pool = pool_for(http3_peer)
    try:
        assert pool.request("GET", "/warmup").status == 200
        conn = pooled_connection(pool)
        protocol = conn._protocol

        def body() -> typing.Iterator[bytes]:
            yield b"first"
            stream_id = conn._stream_id
            observe(conn, lambda: stream_id not in protocol._open_streams)
            assert stream_id in protocol._sending_streams
            assert protocol._events.count(stream_id) == 1
            # The real response has ended while our upload is still open.
            http3_peer.act(lambda peer: peer._quic.stop_stream(stream_id, 0x100))
            observe(conn, lambda: stream_id not in protocol._sending_streams)
            assert protocol._events.count(stream_id) == 1  # Only the response headers.
            yield b"must not be sent"

        response = pool.request("POST", "/reject", body=body())
        assert response.status == 400 and response.data == b""
        assert protocol.is_idle()
        assert not protocol.has_pending_event()
        received: dict[int, int] = {}
        http3_peer.act(lambda peer: received.update(peer.received_body))
        assert list(received.values()) == [len(b"first")]
        assert pool.request("GET", "/after").status == 200
        assert pool.num_connections == 1
    finally:
        pool.close()


def test_http3_discard_blocked_trailers(
    http3_peer: HTTP3Peer,
) -> None:
    pool = pool_for(http3_peer)
    response = None
    try:
        response = pool.request("GET", "/blocked-trailers", preload_content=False)
        assert response.read(1) == b"x"
        conn = response.connection
        assert conn is not None and conn._protocol is not None
        protocol = conn._protocol
        stream_id = typing.cast("Any", response._fp).from_promise.stream_id
        observe(conn, lambda: protocol._http._stream[stream_id].blocked)
        stream = protocol._http._stream[stream_id]
        response.close()
        response = None
        assert not stream.buffer
        assert stream.receiving_ended and stream.sending_ended
        # Deliver the actual encoder instructions after disposing of the response.
        http3_peer.act(
            lambda peer: peer._quic.send_stream_data(
                peer.http._local_encoder_stream_id,
                bytes(peer.pending_encoder),
            )
        )
        next_response = pool.request("GET", "/next")
        assert next_response.status == 200
        assert not protocol.has_pending_event(stream_id=stream_id)
        assert pool.num_connections == 1
    finally:
        if response is not None:
            response.close()
        pool.close()


def test_http3_connection_close_does_not_complete_response(
    http3_peer: HTTP3Peer,
) -> None:
    from urllib3.exceptions import ProtocolError

    pool = pool_for(http3_peer)
    response = None
    try:
        response = pool.request("GET", "/partial-response", preload_content=False)
        assert response.status == 200
        # A transport NO_ERROR close still leaves this HTTP response unfinished.
        http3_peer.act(lambda peer: peer._quic.close(error_code=0, frame_type=0x1C))
        with pytest.raises(ProtocolError):
            response.read()
    finally:
        if response is not None:
            response.close()
        pool.close()


@notWindows()
def test_http3_peer_reset(
    http3_peer: HTTP3Peer,
) -> None:
    from urllib3.exceptions import ProtocolError

    pool = pool_for(http3_peer)
    try:
        with pytest.raises(ProtocolError, match="reset by remote peer"):
            pool.request("GET", "/reset")
        assert pool.request("GET", "/next").status == 200
    finally:
        pool.close()


def test_http3_informational_callback(
    http3_peer: HTTP3Peer,
) -> None:
    received = []

    def early(response: Any) -> None:
        received.append((response.status, response.headers["link"]))

    pool = pool_for(http3_peer)
    try:
        response = pool.request(
            "GET",
            "/early",
            on_early_response=early,
        )
        assert response.status == 200
        assert received == [(103, "</style.css>; rel=preload")]
    finally:
        pool.close()


def test_http3_cancel_unfinished_upload_after_response(
    http3_peer: HTTP3Peer,
) -> None:
    pool = pool_for(http3_peer)
    response = None

    def body() -> Iterator[bytes]:
        # A finite body could finish before the peer's early response arrives.
        # Keep it open until that response interrupts sending, with a watchdog
        # for a broken implementation rather than an assumed network speed.
        deadline = time.monotonic() + 5
        while True:
            assert time.monotonic() < deadline, "The early response did not stop upload"
            yield b"x" * 1024

    try:
        response = pool.request(
            "POST",
            "/upload-response",
            body=body(),
            preload_content=False,
        )
        assert response.status == 413
        conn = response.connection
        assert conn is not None and conn._protocol is not None
        protocol = conn._protocol
        stream_id = typing.cast("Any", response._fp).from_promise.stream_id
        assert stream_id in protocol._sending_streams
        response.close()
        response = None
        assert stream_id not in protocol._sending_streams
        assert stream_id not in protocol._open_streams
        assert pool.request("GET", "/next").status == 200
        assert pool.num_connections == 1
    finally:
        if response is not None:
            response.close()
        pool.close()


@pytest.mark.parametrize("http3_peer", ["early-reject"], indirect=True)
def test_http3_repeated_early_rejections_with_small_connection_credit(
    http3_peer: HTTP3Peer,
) -> None:
    from qh3 import QuicConnectionError

    # A stream can accept the body, but the connection cannot send it in one flight.
    body = b'{"data":"' + b"A" * (300 * 1024) + b'"}'
    pool = pool_for(http3_peer)
    transport = None
    protocol = None
    try:
        for _ in range(6):
            response = pool.request("POST", "/reject", body=body)
            try:
                assert response.status == 400 and response.version == 30
                assert response.data == b""
            finally:
                response.close()
            conn = pooled_connection(pool)
            if transport is None:
                transport, protocol = conn.sock, conn._protocol
            assert transport is not None and conn.sock is transport
            assert conn._protocol is protocol
        assert pool.num_connections == 1
        rejected: dict[int, int] = {}
        http3_peer.act(lambda peer: rejected.update(peer.rejected_at))
        assert len(rejected) == 6
        assert all(0 < received < len(body) for received in rejected.values())
    except (ProtocolError, QuicConnectionError) as exc:
        import qh3

        # Keep older supported dependencies usable without hiding new failures.
        version = getattr(qh3, "__version__", "").partition("+")[0]
        if (
            version in {"2.0.0", "2.0.1", "2.0.2", "2.0.3", "2.0.4"}
            and not hasattr(qh3.QuicConnection, "get_send_buffer_size")
            and "ConnectionSendLimit" in str(exc)
        ):
            pytest.xfail("Known qh3 connection-credit bug; urllib3.future issue #440")
        raise
    finally:
        pool.close()


@pytest.mark.parametrize("http3_peer", ["stop-before-response"], indirect=True)
def test_http3_stop_sending_before_response_headers(
    http3_peer: HTTP3Peer, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = pool_for(http3_peer)
    try:
        assert pool.request("GET", "/warmup").status == 200
        conn = pooled_connection(pool)
        protocol = conn._protocol
        if not hasattr(protocol._quic, "_core"):
            pytest.skip("The ordering gate requires native qh3 STOP_SENDING handling")

        def body() -> Iterator[bytes]:
            yield b"first"
            deadline = time.monotonic() + 10
            while conn._stream_id in protocol._sending_streams:
                assert time.monotonic() < deadline, "STOP_SENDING was not processed"
                conn.peek_and_react(expect_frame=True)
            # Exercise the pre-send check independently of ACK scheduling.
            # The native outstanding-packet table need not become empty here.
            monkeypatch.setattr(
                protocol, "should_wait_remote_flow_control", lambda *args: False
            )
            http3_peer.act(lambda peer: peer.release_rejections())
            yield b"must not be sent"

        response = pool.request("POST", "/reject", body=body())
        try:
            assert response.status == 400 and response.version == 30
        finally:
            response.close()
        assert pool.num_connections == 1
        assert protocol.is_idle()
        received: dict[int, int] = {}
        http3_peer.act(lambda peer: received.update(peer.received_body))
        assert list(received.values()) == [len(b"first")]
        assert pool.request("GET", "/after").status == 200
    finally:
        pool.close()


@pytest.mark.parametrize(
    "operation",
    ["ping", "peek", "abort", "close", "upload", "raw-write", "raw-flow-control"],
)
def test_oversized_quic_packet_does_not_stop_following_packets(
    quic_echo_server: tuple[int, str, typing.Callable[[], None]],
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with HTTPSConnectionPool(
        "localhost",
        quic_echo_server[0],
        ca_cert_data=quic_echo_server[1],
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        disabled_svn={HttpVersion.h11, HttpVersion.h2},
        timeout=5,
        retries=False,
        background_watch_delay=None,
    ) as pool:
        assert pool.request("GET", "/").data == b"ok"
        assert pool.pool is not None
        conn: typing.Any = next(iter(pool.pool._registry.values()))
        protocol = conn._protocol
        sock = conn.sock
        assert protocol is not None and sock is not None
        response = None
        if operation == "abort":
            response = pool.request("GET", "/stream", preload_content=False)
        elif operation.startswith("raw-"):

            class Tunnel(RawExtensionFromHTTP):
                def headers(self, http_version: HttpVersion) -> dict[str, str]:
                    return {":protocol": "bytestream"}

            response = pool.request(
                "CONNECT", "/", preload_content=False, extension=Tunnel()
            )
            assert response.extension is not None
            if operation == "raw-flow-control":
                quic_echo_server[2]()
                assert wait_for_read(sock, timeout=5)
                protocol.ping()
        elif operation == "peek":
            # Two unacknowledged client PINGs trigger our expiry heuristic.
            # Use the peer to make the socket readable, keeping one client PING
            # queued to exercise the send path after the oversized datagram.
            quic_echo_server[2]()
            assert wait_for_read(sock, timeout=5)
            protocol.ping()

        armed = operation not in ("upload", "raw-flow-control")
        injected = False
        rejected: list[int | None] = []
        sent: list[int] = []
        bytes_to_send = protocol.bytes_to_send
        should_wait = protocol.should_wait_remote_flow_control
        send, sendall = socket.socket.send, socket.socket.sendall

        def next_packet() -> bytes:
            nonlocal injected
            if armed and not injected:
                injected = True
                # Force a real OS rejection, regardless of this host's link MTU.
                return b"x" * 65536
            return typing.cast(bytes, bytes_to_send())

        def wait_for_credit(stream_id: int, size: int) -> bool | None:
            nonlocal armed
            if operation in ("upload", "raw-flow-control") and not injected:
                # Enter the ACK-wait send path once. Request headers or the peer
                # PING provide real input, independently of packet pacing.
                armed = True
                return True
            waiting = should_wait(stream_id, size)
            if waiting:
                armed = True
            return typing.cast("bool | None", waiting)

        def record_send(target: socket.socket, data: bytes, flags: int = 0) -> int:
            try:
                result = send(target, data, flags)
            except OSError as exc:
                if target is sock:
                    rejected.append(exc.errno)
                raise
            if target is sock:
                sent.append(result)
            return result

        def record_sendall(target: socket.socket, data: bytes, flags: int = 0) -> None:
            try:
                sendall(target, data, flags)
            except OSError as exc:
                if target is sock:
                    rejected.append(exc.errno)
                raise
            if target is sock:
                sent.append(len(data))

        monkeypatch.setattr(protocol, "bytes_to_send", next_packet)
        monkeypatch.setattr(
            protocol, "should_wait_remote_flow_control", wait_for_credit
        )
        monkeypatch.setattr(socket.socket, "send", record_send)
        monkeypatch.setattr(socket.socket, "sendall", record_sendall)

        if operation == "ping":
            conn.ping()
        elif operation == "peek":
            assert conn.peek_and_react(expect_frame=True)
        elif operation == "abort":
            assert response is not None
            response.close()
        elif operation == "close":
            conn.close()
        elif operation == "upload":
            body = b"x" * 65536
            assert pool.request("POST", "/", body=body).data == body
        else:
            assert response is not None and response.extension is not None
            response.extension.send_payload(b"x" * 1024)

        assert injected, "The intended send path was not reached"
        assert len(rejected) == 1
        assert rejected[0] in {
            errno.EMSGSIZE,
            getattr(errno, "WSAEMSGSIZE", errno.EMSGSIZE),
        }
        assert sent, "The valid packet following the rejected one was not sent"
        if operation != "close":
            # Pool checkout expects a keepalive reply to have arrived already.
            # Finish our freshly issued PING before checking connection reuse.
            deadline = time.monotonic() + 5
            while conn.expect_pong:
                remaining = deadline - time.monotonic()
                assert remaining > 0 and wait_for_read(sock, timeout=remaining)
                conn.peek_and_react(expect_frame=True)
            assert conn.sock is sock and not conn.is_closed
            if response is not None and response.extension is not None:
                response.extension.close()
            assert pool.request("GET", "/").data == b"ok"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux UDP segmentation offload")
@pytest.mark.parametrize("operation", ["headers", "response"])
def test_gso_rejection_retries_real_quic_packets(
    quic_echo_server: tuple[int, str, typing.Callable[..., None]],
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with HTTPSConnectionPool(
        "localhost",
        quic_echo_server[0],
        ca_cert_data=quic_echo_server[1],
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        disabled_svn={HttpVersion.h11, HttpVersion.h2},
        timeout=5,
        retries=False,
        background_watch_delay=None,
    ) as pool:
        assert pool.request("GET", "/").data == b"ok"
        assert pool.pool is not None
        conn: typing.Any = next(iter(pool.pool._registry.values()))
        if not conn._dgram_gso_enabled:
            pytest.skip("Kernel does not expose UDP GSO")
        sock, protocol = conn.sock, conn._protocol
        assert sock is not None and protocol is not None
        response = None
        if operation == "response":
            response = pool.request("GET", "/stream", preload_content=False)
            assert response.read(1) == b"a"

        # A valid unknown HTTP/3 unidirectional stream supplies enough real QUIC
        # packets for batching. The server ignores its unknown stream type.
        def queue_packets(*args: typing.Any) -> None:
            quic = getattr(protocol, "_quic")
            stream_id = quic.get_next_available_stream_id(is_unidirectional=True)
            quic.send_stream_data(stream_id, b"\x21" + b"x" * 8192, end_stream=True)

        if response is not None:
            queue_packets()
        rejected = 0
        sendmsg = socket.socket.sendmsg

        def reject_once(
            current: socket.socket, *args: typing.Any, **kwargs: typing.Any
        ) -> int:
            nonlocal rejected
            if current is sock and rejected == 0:
                rejected += 1
                raise OSError(errno.EIO, "injected GSO driver rejection")
            return sendmsg(current, *args, **kwargs)

        with monkeypatch.context() as failure:
            failure.setattr(socket.socket, "sendmsg", reject_once)
            if response is None:
                assert (
                    pool.request("GET", "/", on_post_connection=queue_packets).data
                    == b"ok"
                )
            else:
                quic_echo_server[2](finish_body=True)
                assert response.read() == b"a" * 1023
        assert rejected == 1
        assert not conn._dgram_gso_enabled
        assert conn.sock is sock
        assert pool.request("GET", "/next").data == b"ok"


@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize("packet_loss", [False, True])
def test_flow_control_accepts_batched_quic_input(
    quic_echo_server: tuple[int, str, typing.Callable[..., None]],
    raw: bool,
    packet_loss: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib3.backend import hface
    from urllib3.util.sub_timeout import AsyncSubTimeout, SubTimeout

    pool = HTTPSConnectionPool(
        "localhost",
        quic_echo_server[0],
        ca_cert_data=quic_echo_server[1],
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        disabled_svn={HttpVersion.h11, HttpVersion.h2},
        timeout=5,
        retries=False,
        background_watch_delay=None,
    )
    response = None
    try:
        assert pool.request("GET", "/").status == 200
        assert pool.pool is not None
        conn: typing.Any = next(iter(pool.pool._registry.values()))
        sock, protocol = conn.sock, conn._protocol
        assert sock is not None and protocol is not None
        batches = flow_waits = timer_firings = 0
        waiting = protocol.should_wait_remote_flow_control
        finish_timeout, finish_async_timeout = (
            SubTimeout.__exit__,
            AsyncSubTimeout.__aexit__,
        )

        def resume_after_timer(sub: typing.Any) -> None:
            nonlocal timer_firings
            if packet_loss and flow_waits and sub.sock is sock and sub.timer_fired:
                timer_firings += 1
                if timer_firings == 1:
                    # Resume the actual peer only after a real QUIC deadline
                    # fires. No fixed sleep or simulated timeout is needed.
                    quic_echo_server[2](pause_reads=False, hold_transmit=False)

        def observe_timeout(sub: typing.Any, *args: typing.Any) -> bool:
            result = finish_timeout(sub, *args)
            resume_after_timer(sub)
            return result

        async def observe_async_timeout(sub: typing.Any, *args: typing.Any) -> bool:
            result = await finish_async_timeout(sub, *args)
            resume_after_timer(sub)
            return result

        def observe_credit(stream_id: int, amount: int) -> bool | None:
            nonlocal flow_waits
            result = waiting(stream_id, amount)
            flow_waits += result is True
            if result is True and flow_waits == 1 and not packet_loss:
                quic_echo_server[2](hold_transmit=False)
            return typing.cast("bool | None", result)

        def require_ack_wait() -> None:
            # QUIC buffers writes, so a small stream window alone cannot force
            # this loop. Keep actual PING packets unacknowledged until the
            # backend enters its wait (or its real timer fires in the loss case).
            quic_echo_server[2](pause_reads=packet_loss, hold_transmit=True)
            deadline = time.monotonic() + 5
            sent = 0
            while sent < 2 or waiting(conn._stream_id, 1024) is not True:
                protocol._quic.send_ping(sent)
                data = protocol.bytes_to_send()
                while not data:
                    assert time.monotonic() < deadline, "QUIC did not emit a PING"
                    time.sleep(0)
                    data = protocol.bytes_to_send()
                sock.sendall(data)
                sent += 1
                assert time.monotonic() < deadline, "QUIC did not enter ACK wait"

        def batch(data: bytes | list[bytes]) -> bytes | list[bytes]:
            nonlocal batches
            if data:
                batches += 1
                return data if isinstance(data, list) else [data]
            return data

        target: typing.Any
        if conn._dgram_gro_enabled:
            target, name = hface, "sync_recv_gro"
        else:
            target, name = type(sock), "recv"
        receive = getattr(target, name)

        def receive_sync(
            current: typing.Any, *args: typing.Any, **kwargs: typing.Any
        ) -> typing.Any:
            data = receive(current, *args, **kwargs)
            return batch(data) if current is sock else data

        with monkeypatch.context() as transport:
            transport.setattr(target, name, receive_sync)
            transport.setattr(
                protocol, "should_wait_remote_flow_control", observe_credit
            )
            transport.setattr(SubTimeout, "__exit__", observe_timeout)
            transport.setattr(AsyncSubTimeout, "__aexit__", observe_async_timeout)
            body = b"x" * 65536
            if raw:
                extension = RawExtensionFromHTTP()
                response = pool.request(
                    "CONNECT",
                    "/",
                    preload_content=False,
                    headers={":protocol": "bytestream"},
                    extension=extension,
                )
                require_ack_wait()
                for offset in range(0, len(body), 1024):
                    extension.send_payload(body[offset : offset + 1024])
                received = bytearray()
                while len(received) < len(body):
                    chunk = extension.next_payload()
                    assert chunk
                    received.extend(chunk)
                assert received == body
                extension.close()
            else:
                require_ack_wait()
                result = pool.request("POST", "/", body=body, preload_content=False)
                assert result.read() == body
        assert batches > 0 and flow_waits > 0
        if packet_loss:
            assert timer_firings > 0
        assert pool.request("GET", "/next").status == 200
    finally:
        quic_echo_server[2](pause_reads=False, hold_transmit=False)
        if response is not None:
            response.close()
        pool.close()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux UDP GRO receive path")
def test_quic_peek_collects_a_second_received_batch(
    quic_echo_server: tuple[int, str, typing.Callable[..., None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib3.backend import hface

    with HTTPSConnectionPool(
        "localhost",
        quic_echo_server[0],
        ca_cert_data=quic_echo_server[1],
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        disabled_svn={HttpVersion.h11, HttpVersion.h2},
        timeout=5,
        retries=False,
        background_watch_delay=None,
    ) as pool:
        assert pool.request("GET", "/").data == b"ok"
        assert pool.pool is not None
        conn: typing.Any = next(iter(pool.pool._registry.values()))
        if not conn._dgram_gro_enabled:
            pytest.skip("Kernel does not expose UDP GRO")
        sock = conn.sock
        assert sock is not None
        receive = getattr(hface, "sync_recv_gro")
        batches = 0

        def next_batch(current: socket.socket, size: int) -> bytes | list[bytes]:
            nonlocal batches
            data: bytes | list[bytes] = receive(current, size)
            if current is sock and data:
                batches += 1
                if batches == 1:
                    # The next real packet arrives only after the first batch
                    # has been read, independently of kernel packet coalescing.
                    quic_echo_server[2]()
                return data if isinstance(data, list) else [data]
            return data

        quic_echo_server[2]()
        assert wait_for_read(sock, timeout=5)
        with monkeypatch.context() as transport:
            transport.setattr(hface, "sync_recv_gro", next_batch)
            assert conn.peek_and_react(expect_frame=True)
        assert batches >= 2
        assert pool.request("GET", "/next").data == b"ok"
