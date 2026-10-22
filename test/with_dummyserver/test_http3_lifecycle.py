from __future__ import annotations

import asyncio
import inspect
import time
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple, cast

import pytest
import trustme
from tornado.platform.asyncio import AsyncIOLoop

from dummyserver.server import DEFAULT_CA, run_loop_in_thread
from urllib3 import AsyncHTTPSConnectionPool, HTTPSConnectionPool, HttpVersion
from urllib3._async.connection import AsyncHTTPSConnection
from urllib3.connection import HTTPSConnection

from .test_http2_lifecycle import HTTP2Peer, http2_peer as http2_peer


class HTTP3Peer(NamedTuple):
    port: int
    ca_data: str
    act: Callable[[Callable[[Any], None]], None]


@pytest.fixture
def http3_peer(tmp_path: Path, requires_http3: None) -> Iterator[HTTP3Peer]:
    """A real QUIC peer with actions executed on its owning event loop."""
    from qh3 import H3Connection, QuicConfiguration, h3_events
    from qh3.asyncio.protocol import QuicConnectionProtocol
    from qh3.asyncio.server import serve

    errors: list[BaseException] = []

    class Peer(QuicConnectionProtocol):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.http = H3Connection(self._quic)
            self.blackhole = False
            self.pending_encoder = bytearray()
            self.pending_headers: list[Any] = []

        def datagram_received(self, data: bytes | str, addr: Any) -> None:
            if not self.blackhole:
                super().datagram_received(data, addr)

        def quic_event_received(self, event: Any) -> None:
            try:
                events = self.http.handle_event(event)
                if self.http.received_settings is not None:
                    events = self.pending_headers + events
                    self.pending_headers = []
                for received in events:
                    if not isinstance(received, h3_events.HeadersReceived):
                        continue
                    headers = dict(received.headers)
                    stream_id = received.stream_id
                    if (
                        headers[b":path"] == b"/blocked-trailers"
                        and self.http.received_settings is None
                    ):
                        # Request and control streams can arrive in either order.
                        # Dynamic QPACK encoding needs the client's SETTINGS.
                        self.pending_headers.append(received)
                        continue
                    if headers[b":path"] in (b"/reset", b"/fallback"):
                        self._quic.reset_stream(
                            stream_id,
                            0x110 if headers[b":path"] == b"/fallback" else 0x10C,
                        )
                        continue
                    if headers[b":path"] == b"/early":
                        self.http.send_headers(
                            stream_id,
                            [
                                (b":status", b"103"),
                                (b"link", b"</style.css>; rel=preload"),
                            ],
                        )
                    response_headers = [(b":status", b"200")]
                    if headers.get(b":protocol") == b"websocket":
                        import base64
                        import hashlib

                        token = (
                            headers[b"sec-websocket-key"]
                            + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                        )
                        response_headers.append(
                            (
                                b"sec-websocket-accept",
                                base64.b64encode(hashlib.sha1(token).digest()),
                            )
                        )
                    if headers[b":path"] == b"/partial-response":
                        response_headers.append((b"content-length", b"4"))
                    elif headers[b":path"] == b"/upload-response":
                        response_headers = [
                            (b":status", b"413"),
                            (b"content-length", b"2"),
                        ]
                    self.http.send_headers(
                        stream_id,
                        response_headers,
                        end_stream=headers[b":path"] == b"/empty-tunnel",
                    )
                    if headers[b":method"] == b"CONNECT":
                        continue
                    if headers[b":path"] in (b"/partial-response", b"/upload-response"):
                        self.http.send_data(stream_id, b"ab", end_stream=False)
                    elif headers[b":path"] == b"/blocked-trailers":
                        self.http.send_data(stream_id, b"x", end_stream=False)
                        # Hold genuine encoder instructions while sending the trailer
                        # HEADERS and FIN. Both QPACK endpoints retain their real state.
                        send = self._quic.send_stream_data

                        def delay_encoder(
                            stream: int, data: bytes, end_stream: bool = False
                        ) -> None:
                            if stream == self.http._local_encoder_stream_id:
                                self.pending_encoder.extend(data)
                            else:
                                send(stream, data, end_stream=end_stream)

                        setattr(self._quic, "send_stream_data", delay_encoder)
                        try:
                            self.http.send_headers(
                                stream_id,
                                [(b"x-some-trailer", b"foo")] * 8,
                                end_stream=True,
                            )
                        finally:
                            setattr(self._quic, "send_stream_data", send)
                        assert self.pending_encoder, (
                            "Encoder did not create a dynamic entry"
                        )
                    else:
                        self.http.send_data(stream_id, b"ok", end_stream=True)
                self.transmit()
            except BaseException as exc:
                errors.append(exc)
                self.close()

    ca = trustme.CA()
    cert = ca.issue_cert("localhost")
    cert_file, key_file = tmp_path / "server.pem", tmp_path / "server.key"
    cert.cert_chain_pems[0].write_to_path(cert_file)
    cert.private_key_pem.write_to_path(key_file)

    async def start() -> Any:
        config = QuicConfiguration(
            is_client=False, alpn_protocols=["h3"], max_datagram_frame_size=65536
        )
        config.load_cert_chain(str(cert_file), str(key_file))
        return await serve("127.0.0.1", 0, configuration=config, create_protocol=Peer)

    with run_loop_in_thread() as io_loop:
        loop = cast(AsyncIOLoop, io_loop).asyncio_loop
        server = asyncio.run_coroutine_threadsafe(start(), loop).result(10)

        def act(action: Callable[[Any], None]) -> None:
            async def run() -> None:
                for peer in set(server._protocols.values()):
                    action(peer)
                    peer.transmit()

            asyncio.run_coroutine_threadsafe(run(), loop).result(10)

        try:
            yield HTTP3Peer(
                server._transport.get_extra_info("sockname")[1],
                ca.cert_pem.bytes().decode(),
                act,
            )
        finally:

            async def stop() -> None:
                peers = set(server._protocols.values())
                for peer in peers:
                    peer.blackhole = False
                    peer.close()
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*(peer.wait_closed() for peer in peers)), 5
                    )
                finally:
                    server.close()

            asyncio.run_coroutine_threadsafe(stop(), loop).result(10)
            assert not errors, errors


async def call(method: Any, *args: Any, **kwargs: Any) -> Any:
    result = method(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


def pool_for(peer: HTTP3Peer, asynchronous: bool) -> Any:
    return (AsyncHTTPSConnectionPool if asynchronous else HTTPSConnectionPool)(
        "localhost",
        peer.port,
        resolver="in-memory://default?hosts=localhost:127.0.0.1",
        ca_cert_data=peer.ca_data,
        timeout=5,
        retries=False,
        disabled_svn={HttpVersion.h11, HttpVersion.h2},
        background_watch_delay=None,
    )


async def observe(conn: Any, ready: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while not ready():
        assert time.monotonic() < deadline, "Peer event did not reach the client"
        await call(conn.peek_and_react, expect_frame=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("retirement", ["unanswered-pings", "goaway", "idle-timeout"])
async def test_http3_connection_retirement(
    http3_peer: HTTP3Peer,
    asynchronous: bool,
    retirement: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = pool_for(http3_peer, asynchronous)
    try:
        assert (await call(pool.request, "GET", "/")).status == 200
        conn = next(iter(pool.pool._registry.values()))
        assert isinstance(conn, (HTTPSConnection, AsyncHTTPSConnection))
        sock, protocol = conn.sock, conn._protocol
        assert protocol is not None
        if retirement == "unanswered-pings":
            http3_peer.act(lambda peer: setattr(peer, "blackhole", True))
            await call(conn.ping)
            await call(conn.ping)
        elif retirement == "goaway":
            from qh3.h3.connection import FrameType, encode_frame
            from qh3._hazmat import encode_uint_var

            http3_peer.act(
                lambda peer: peer._quic.send_stream_data(
                    peer.http._local_control_stream_id,
                    encode_frame(FrameType.GOAWAY, encode_uint_var(4)),
                )
            )
            await observe(conn, lambda: bool(getattr(protocol, "_goaway_to_honor")))
        else:
            from urllib3.contrib.hface.protocols.http3 import _qh3

            # Advance the clock supplied to the native QUIC timer after a real handshake.
            expired = time.time() + 600
            with monkeypatch.context() as clock:
                clock.setattr(_qh3, "monotonic", lambda: expired)
                assert not conn.is_connected
        assert not conn.is_connected
        assert not conn.is_saturated
        assert (await call(pool.request, "GET", "/next")).status == 200
        assert conn.sock is not sock
    finally:
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_http3_receive_error_retires_connection(
    http3_peer: HTTP3Peer, asynchronous: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = pool_for(http3_peer, asynchronous)
    try:
        assert (await call(pool.request, "GET", "/")).status == 200
        conn = next(iter(pool.pool._registry.values()))
        sock = conn.sock
        assert sock is not None
        receive_method = (
            "recvmsg" if not asynchronous and conn._dgram_gro_enabled else "recv"
        )
        receive = getattr(type(sock), receive_method)

        def fail_receive(current: Any, *args: Any, **kwargs: Any) -> Any:
            if current is sock:
                raise ConnectionResetError("injected receive failure")
            return receive(current, *args, **kwargs)

        # Inject only the OS receive failure after establishing the real QUIC connection.
        with monkeypatch.context() as failure:
            failure.setattr(type(sock), receive_method, fail_receive)
            assert await call(conn.peek_and_react, expect_frame=True)
        assert not conn.is_connected
        assert (await call(pool.request, "GET", "/next")).status == 200
        assert conn.sock is not sock
    finally:
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_http3_empty_raw_response_closes_after_goaway(
    http3_peer: HTTP3Peer, asynchronous: bool
) -> None:
    from qh3.h3.connection import FrameType, encode_frame
    from qh3._hazmat import encode_uint_var
    from urllib3.contrib.webextensions.raw import RawExtensionFromHTTP
    from urllib3.contrib.webextensions._async.raw import AsyncRawExtensionFromHTTP

    pool = pool_for(http3_peer, asynchronous)
    response = None
    try:
        extension = (
            AsyncRawExtensionFromHTTP() if asynchronous else RawExtensionFromHTTP()
        )
        response = await call(
            pool.request,
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
        await observe(conn, lambda: bool(getattr(protocol, "_goaway_to_honor")))
        await call(extension.close)
        assert conn.sock is None
    finally:
        if response is not None:
            await call(response.close)
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_http3_response_close_after_explicit_connection_close(
    http3_peer: HTTP3Peer, asynchronous: bool
) -> None:
    pool = pool_for(http3_peer, asynchronous)
    response = None
    try:
        response = await call(
            pool.request, "GET", "/partial-response", preload_content=False
        )
        conn = next(iter(pool.pool._registry.values()))
        await call(conn.close)
        await call(response.close)
        assert conn.sock is None and response.closed
    finally:
        if response is not None:
            await call(response.close)
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_http3_batch_engine_error_is_translated(
    http3_peer: HTTP3Peer, asynchronous: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qh3 import QuicConnectionError
    from urllib3.backend import hface
    from urllib3.exceptions import ProtocolError

    pool = pool_for(http3_peer, asynchronous)
    injected = False
    try:
        promise = await call(pool.request, "GET", "/", multiplexed=True)
        conn = next(iter(pool.pool._registry.values()))
        sock, protocol = conn.sock, conn._protocol
        assert sock is not None and protocol is not None
        target: Any
        if not asynchronous and conn._dgram_gro_enabled:
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

        async def received_async_batch(current: Any, *args: Any, **kwargs: Any) -> Any:
            result = await receive(current, *args, **kwargs)
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
            failure.setattr(
                target, name, received_async_batch if asynchronous else received_batch
            )
            failure.setattr(protocol._quic, "receive_many_datagrams", engine_failure)
            with pytest.raises(ProtocolError, match="injected QUIC engine failure"):
                await call(pool.get_response, promise=promise)
        assert injected
        # The injected error did not alter the QUIC state. A caller can resume
        # collection when the real peer retransmits the unacknowledged packet.
        assert (await call(pool.get_response, promise=promise)).status == 200
        assert (await call(pool.request, "GET", "/next")).status == 200
    finally:
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("http2_peer", ["require-http1"], indirect=True)
async def test_http3_then_http2_fallback_replaces_rejected_connections(
    http3_peer: HTTP3Peer, http2_peer: HTTP2Peer, asynchronous: bool
) -> None:
    pool = (AsyncHTTPSConnectionPool if asynchronous else HTTPSConnectionPool)(
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
        response = await call(pool.request, "GET", "/fallback")
        assert response.status == 200 and response.version == 11
        assert pool.num_connections == 3
        assert isinstance(http2_peer.requests.get(timeout=5), list)
        assert http2_peer.requests.get(timeout=5).startswith(
            b"GET /fallback HTTP/1.1\r\n"
        )
    finally:
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_raw_stream_bounded_reads_preserve_buffer_and_eof(
    http3_peer: HTTP3Peer, asynchronous: bool
) -> None:
    from urllib3.contrib.webextensions.raw import RawExtensionFromHTTP
    from urllib3.contrib.webextensions._async.raw import AsyncRawExtensionFromHTTP

    pool = pool_for(http3_peer, asynchronous)
    response = None
    try:
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
        dsa = extension._dsa
        assert dsa is not None and not dsa.closed
        stream_id = response._fp.from_promise.stream_id
        http3_peer.act(
            lambda peer: peer.http.send_data(stream_id, b"abcdef", end_stream=False)
        )

        # Exercise the real low-level socket-compatible API. No read callback
        # or receive buffer is fabricated; the bytes came from the QUIC peer.
        async def read_expected(expected: bytes, size: int | None) -> None:
            data = bytearray()
            while len(data) < len(expected):
                amount = None if size is None else min(size, len(expected) - len(data))
                chunk, ended, _ = await call(dsa.recv_extended, amount)
                assert chunk and not ended
                data.extend(chunk)
            assert data == expected

        await read_expected(b"ab", 2)
        await read_expected(b"cd", 2)
        await read_expected(b"ef", None)
        http3_peer.act(
            lambda peer: peer.http.send_data(stream_id, b"", end_stream=True)
        )
        data, ended, _ = await call(dsa.recv_extended, 2)
        assert data == b"" and ended
        with pytest.raises(OSError, match="closed"):
            await call(dsa.recv_extended, 1)
        await call(extension.close)
        assert dsa.closed
        with pytest.raises(OSError, match="write not permitted"):
            await call(dsa.sendall, b"late")
    finally:
        if response is not None:
            await call(response.close)
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("peer_stops_upload", [False, True])
async def test_http3_close_raw_stream_preserves_connection(
    http3_peer: HTTP3Peer,
    asynchronous: bool,
    peer_stops_upload: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib3.contrib.webextensions.raw import RawExtensionFromHTTP
    from urllib3.contrib.webextensions._async.raw import AsyncRawExtensionFromHTTP

    pool = pool_for(http3_peer, asynchronous)
    response = None
    try:
        extension = (
            AsyncRawExtensionFromHTTP() if asynchronous else RawExtensionFromHTTP()
        )
        if not asynchronous:
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
        response = await call(
            pool.request,
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
        stream_id = response._fp.from_promise.stream_id
        if peer_stops_upload:
            http3_peer.act(lambda peer: peer._quic.stop_stream(stream_id, 0x10C))
            await observe(conn, lambda: stream_id not in protocol._sending_streams)
        await call(response.close)
        response = None
        assert not protocol._open_streams
        assert not protocol._sending_streams
        assert (await call(pool.request, "GET", "/next")).status == 200
        assert pool.num_connections == 1
    finally:
        if response is not None:
            await call(response.close)
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_http3_discard_blocked_trailers(
    http3_peer: HTTP3Peer, asynchronous: bool
) -> None:
    pool = pool_for(http3_peer, asynchronous)
    response = None
    try:
        response = await call(
            pool.request, "GET", "/blocked-trailers", preload_content=False
        )
        assert await call(response.read, 1) == b"x"
        conn = response.connection
        assert conn is not None and conn._protocol is not None
        protocol = conn._protocol
        stream_id = response._fp.from_promise.stream_id
        await observe(conn, lambda: protocol._http._stream[stream_id].blocked)
        stream = protocol._http._stream[stream_id]
        await call(response.close)
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
        next_response = await call(pool.request, "GET", "/next")
        assert next_response.status == 200
        assert not protocol.has_pending_event(stream_id=stream_id)
        assert pool.num_connections == 1
    finally:
        if response is not None:
            await call(response.close)
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_http3_connection_close_does_not_complete_response(
    http3_peer: HTTP3Peer, asynchronous: bool
) -> None:
    from urllib3.exceptions import ProtocolError

    pool = pool_for(http3_peer, asynchronous)
    response = None
    try:
        response = await call(
            pool.request, "GET", "/partial-response", preload_content=False
        )
        assert response.status == 200
        # A transport NO_ERROR close still leaves this HTTP response unfinished.
        http3_peer.act(lambda peer: peer._quic.close(error_code=0, frame_type=0x1C))
        with pytest.raises(ProtocolError):
            await call(response.read)
    finally:
        if response is not None:
            await call(response.close)
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_http3_peer_reset(http3_peer: HTTP3Peer, asynchronous: bool) -> None:
    from urllib3.exceptions import ProtocolError

    pool = pool_for(http3_peer, asynchronous)
    try:
        with pytest.raises(ProtocolError, match="reset by remote peer"):
            await call(pool.request, "GET", "/reset")
        assert (await call(pool.request, "GET", "/next")).status == 200
    finally:
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_http3_informational_callback(
    http3_peer: HTTP3Peer, asynchronous: bool
) -> None:
    received = []

    def early(response: Any) -> None:
        received.append((response.status, response.headers["link"]))

    async def async_early(response: Any) -> None:
        early(response)

    pool = pool_for(http3_peer, asynchronous)
    try:
        response = await call(
            pool.request,
            "GET",
            "/early",
            on_early_response=async_early if asynchronous else early,
        )
        assert response.status == 200
        assert received == [(103, "</style.css>; rel=preload")]
    finally:
        await call(pool.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_http3_cancel_unfinished_upload_after_response(
    http3_peer: HTTP3Peer, asynchronous: bool
) -> None:
    pool = pool_for(http3_peer, asynchronous)
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
        response = await call(
            pool.request,
            "POST",
            "/upload-response",
            body=body(),
            preload_content=False,
        )
        assert response.status == 413
        conn = response.connection
        assert conn is not None and conn._protocol is not None
        protocol = conn._protocol
        stream_id = response._fp.from_promise.stream_id
        assert stream_id in protocol._sending_streams
        await call(response.close)
        response = None
        assert stream_id not in protocol._sending_streams
        assert stream_id not in protocol._open_streams
        assert (await call(pool.request, "GET", "/next")).status == 200
        assert pool.num_connections == 1
    finally:
        if response is not None:
            await call(response.close)
        await call(pool.close)
