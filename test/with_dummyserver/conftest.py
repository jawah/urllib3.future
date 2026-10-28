from __future__ import annotations

import asyncio
import contextlib
import functools
import gzip
import queue
import socket
import ssl
import threading
import typing
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple, cast

import pytest
import trustme
from jh2.config import H2Configuration  # type: ignore[import-untyped]
from jh2.connection import H2Connection  # type: ignore[import-untyped]
from jh2.events import (  # type: ignore[import-untyped]
    DataReceived,
    PingReceived,
    RequestReceived,
    StreamEnded,
)
from jh2.settings import SettingCodes, Settings  # type: ignore[import-untyped]
from tornado.platform.asyncio import AsyncIOLoop

from dummyserver.server import DEFAULT_CERTS, run_loop_in_thread
from urllib3.backend.hface import _HAS_HTTP3_SUPPORT


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
    # A fresh context advertises no ALPN; OpenSSL 1.0.2 rejects an empty list.
    if mode not in ("no-alpn", "no-compatible"):
        context.set_alpn_protocols(["h2"])
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
                                            if not (
                                                exc.errno == 0
                                                # The peer may abort/reset while we finish shutdown.
                                                or isinstance(
                                                    exc,
                                                    (ssl.SSLEOFError, ConnectionError),
                                                )
                                                or getattr(exc, "reason", None)
                                                == "APPLICATION_DATA_AFTER_CLOSE_NOTIFY"
                                                # Older OpenSSL may label this KRB5_S_INIT.
                                                or "application data after close notify"
                                                in str(exc).lower()
                                            ):
                                                raise
                                    break
            except BaseException as exc:
                # This client intentionally aborts immediately after TLS when
                # no enabled HTTP protocol remains. It must report RuntimeError.
                if mode != "no-compatible" or not isinstance(
                    exc,
                    (
                        ssl.SSLError,
                        ConnectionResetError,
                        ConnectionAbortedError,
                        BrokenPipeError,
                    ),
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


class HTTP3Peer(NamedTuple):
    port: int
    ca_data: str
    act: Callable[[Callable[[Any], None]], None]


@pytest.fixture
def http3_peer(
    tmp_path: Path, requires_http3: None, request: pytest.FixtureRequest
) -> Iterator[HTTP3Peer]:
    """A real QUIC peer with actions executed on its owning event loop."""
    from qh3 import H3Connection, QuicConfiguration, h3_events
    from qh3.asyncio.protocol import QuicConnectionProtocol
    from qh3.asyncio.server import serve

    errors: list[BaseException] = []
    mode = getattr(request, "param", "normal")

    class Peer(QuicConnectionProtocol):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.http = H3Connection(self._quic)
            self.blackhole = False
            self.pending_encoder = bytearray()
            self.pending_headers: list[Any] = []
            self.received_body: dict[int, int] = {}
            self.rejected_at: dict[int, int] = {}
            self.held_rejections: set[int] = set()

        def release_rejections(self) -> None:
            for stream_id in self.held_rejections:
                self.http.send_headers(
                    stream_id,
                    [(b":status", b"400"), (b"content-length", b"0")],
                    end_stream=True,
                )
            self.held_rejections.clear()

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
                    if mode in ("early-reject", "stop-before-response") and isinstance(
                        received, h3_events.DataReceived
                    ):
                        stream_id = received.stream_id
                        self.received_body[stream_id] = self.received_body.get(
                            stream_id, 0
                        ) + len(received.data)
                        if stream_id not in self.rejected_at:
                            self.rejected_at[stream_id] = self.received_body[stream_id]
                            if mode == "stop-before-response":
                                self._quic.stop_stream(stream_id, 0x100)
                                self.held_rejections.add(stream_id)
                            else:
                                self.http.send_headers(
                                    stream_id,
                                    [(b":status", b"400"), (b"content-length", b"0")],
                                    end_stream=True,
                                )
                        continue
                    if not isinstance(received, h3_events.HeadersReceived):
                        continue
                    headers = dict(received.headers)
                    stream_id = received.stream_id
                    if (
                        mode in ("early-reject", "stop-before-response")
                        and headers[b":method"] == b"POST"
                    ):
                        continue  # Reject only after a real body fragment has arrived.
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
            is_client=False,
            alpn_protocols=["h3"],
            max_datagram_frame_size=65536,
        )
        if mode in ("early-reject", "stop-before-response"):
            config.max_data = 4096
            config.max_stream_data = 1024 * 1024
        config.load_cert_chain(str(cert_file), str(key_file))
        server = await serve("127.0.0.1", 0, configuration=config, create_protocol=Peer)
        if hasattr(socket, "SIO_UDP_CONNRESET"):
            # Retired clients can reject late packets. Older Proactor loops
            # stop receiving after the resulting ICMP error on this shared socket.
            assert server._transport is not None
            sock = server._transport.get_extra_info("socket")
            getattr(sock, "_sock", sock).ioctl(socket.SIO_UDP_CONNRESET, False)
        return server

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


@pytest.fixture
def quic_echo_server(
    tmp_path: Path,
) -> typing.Iterator[tuple[int, str, typing.Callable[..., None]]]:
    if not _HAS_HTTP3_SUPPORT():
        pytest.skip("HTTP/3 unavailable")
    from qh3 import H3Connection, QuicConfiguration, h3_events
    from qh3.asyncio.protocol import QuicConnectionProtocol
    from qh3.asyncio.server import serve

    class Peer(QuicConnectionProtocol):
        def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
            super().__init__(*args, **kwargs)
            self.http = H3Connection(self._quic)
            self.bodies: dict[int, bytearray] = {}
            self.tunnels: set[int] = set()
            self.streaming: set[int] = set()
            self.paused = False
            self.hold_transmit = False

        def transmit(self) -> None:
            if not self.hold_transmit:
                super().transmit()

        def datagram_received(self, data: bytes | str, addr: typing.Any) -> None:
            if not self.paused:
                super().datagram_received(data, addr)

        def quic_event_received(self, event: typing.Any) -> None:
            for received in self.http.handle_event(event):
                if not isinstance(
                    received, (h3_events.HeadersReceived, h3_events.DataReceived)
                ):
                    continue
                stream_id = received.stream_id
                if isinstance(received, h3_events.HeadersReceived):
                    headers = dict(received.headers)
                    if headers[b":method"] == b"CONNECT":
                        self.tunnels.add(stream_id)
                        self.http.send_headers(stream_id, [(b":status", b"200")])
                    elif headers[b":path"] == b"/stream":
                        self.streaming.add(stream_id)
                        self.http.send_headers(
                            stream_id,
                            [(b":status", b"200"), (b"content-length", b"1024")],
                        )
                        self.http.send_data(stream_id, b"a", end_stream=False)
                    elif received.stream_ended:
                        self.reply(stream_id, b"ok")
                    else:
                        self.bodies[stream_id] = bytearray()
                elif isinstance(received, h3_events.DataReceived):
                    if stream_id in self.tunnels:
                        self.http.send_data(
                            stream_id, received.data, end_stream=received.stream_ended
                        )
                    else:
                        self.bodies[stream_id].extend(received.data)
                        if received.stream_ended:
                            self.reply(stream_id, bytes(self.bodies.pop(stream_id)))
            self.transmit()

        def reply(self, stream_id: int, body: bytes) -> None:
            self.http.send_headers(
                stream_id,
                [(b":status", b"200"), (b"content-length", str(len(body)).encode())],
            )
            self.http.send_data(stream_id, body, end_stream=True)

    ca = trustme.CA()
    cert = ca.issue_cert("localhost")
    cert_path, key_path = tmp_path / "server.pem", tmp_path / "server.key"
    cert.cert_chain_pems[0].write_to_path(cert_path)
    cert.private_key_pem.write_to_path(key_path)

    async def start() -> typing.Any:
        config = QuicConfiguration(
            is_client=False,
            alpn_protocols=["h3"],
            max_datagram_frame_size=65536,
            max_stream_data=4096,
        )
        config.load_cert_chain(str(cert_path), str(key_path))
        return await serve("127.0.0.1", 0, configuration=config, create_protocol=Peer)

    with run_loop_in_thread() as io_loop:
        loop = typing.cast(AsyncIOLoop, io_loop).asyncio_loop
        server = asyncio.run_coroutine_threadsafe(start(), loop).result(5)
        try:

            def peer_ping(
                *,
                finish_body: bool = False,
                pause_reads: bool | None = None,
                hold_transmit: bool | None = None,
            ) -> None:
                async def send() -> None:
                    for peer in set(server._protocols.values()):
                        if pause_reads is not None:
                            peer.paused = pause_reads
                        if hold_transmit is not None:
                            peer.hold_transmit = hold_transmit
                        if finish_body:
                            for stream_id in peer.streaming:
                                peer.http.send_data(
                                    stream_id, b"a" * 1023, end_stream=True
                                )
                            peer.streaming.clear()
                        peer._quic.send_ping(0)
                        peer.transmit()

                asyncio.run_coroutine_threadsafe(send(), loop).result(5)

            yield (
                server._transport.get_extra_info("sockname")[1],
                ca.cert_pem.bytes().decode(),
                peer_ping,
            )
        finally:

            async def stop() -> None:
                peers = set(server._protocols.values())
                for peer in peers:
                    peer.paused = False
                    peer.hold_transmit = False
                    peer.close()
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*(peer.wait_closed() for peer in peers)), 5
                    )
                finally:
                    server.close()

            asyncio.run_coroutine_threadsafe(stop(), loop).result(10)


# WHATWG HTML 9.2.5–9.2.6: event-stream framing and interpretation.
SSE_EVENT_CASES = [
    pytest.param(
        b"data: first\ndata: second\n\n",
        [("message", "first\nsecond", "", None)],
        id="issue-439",
    ),
    pytest.param(
        b"data: first\ndata:\ndata: third\ndata:\n\n",
        [("message", "first\n\nthird\n", "", None)],
        id="empty-data-lines",
    ),
    pytest.param(b"data\n\n", [("message", "", "", None)], id="colonless-data"),
    pytest.param(
        b"data: first\rdata: second\r\r",
        [("message", "first\nsecond", "", None)],
        id="cr-lines",
    ),
    pytest.param(
        b"data: first\r\ndata: second\n\r",
        [("message", "first\nsecond", "", None)],
        id="mixed-lines",
    ),
    pytest.param(
        b"data: first\r\rdata: second\n\n",
        [("message", "first", "", None), ("message", "second", "", None)],
        id="mixed-events",
    ),
    pytest.param(
        b"\xef\xbb\xbfdata: first\n\ndata: second\n\n",
        [("message", "first", "", None), ("message", "second", "", None)],
        id="leading-bom",
    ),
    pytest.param(
        "data: a\ufeffb\n\n".encode(),
        [("message", "a\ufeffb", "", None)],
        id="embedded-bom",
    ),
    pytest.param(
        "data: a\u0085b\u2028c\u2029d\vnext\ffinal\n\n".encode(),
        [("message", "a\u0085b\u2028c\u2029d\vnext\ffinal", "", None)],
        id="unicode-is-not-line-ending",
    ),
    pytest.param(
        b"data: before\xffafter\n\n",
        [("message", "before\ufffdafter", "", None)],
        id="invalid-utf8",
    ),
    pytest.param(
        b"data: ok\n\ndata: incomplete \xe2\x82",
        [("message", "ok", "", None)],
        id="incomplete-utf8-at-eof",
    ),
    pytest.param(b"data: discard\n", [], id="no-final-blank-line"),
    pytest.param(b"\xef\xbb\xbf", [], id="only-bom"),
    pytest.param(
        b"id: cursor\ndata: first\n\nid:\ndata: second\n\ndata: third\n\n",
        [
            ("message", "first", "cursor", None),
            ("message", "second", "", None),
            ("message", "third", "", None),
        ],
        id="empty-id-reset",
    ),
    pytest.param(
        b"id: cursor\ndata: first\n\nid\ndata: second\n\ndata: third\n\n",
        [
            ("message", "first", "cursor", None),
            ("message", "second", "", None),
            ("message", "third", "", None),
        ],
        id="colonless-id-reset",
    ),
    pytest.param(
        b"id: cursor\ndata: first\n\nid: bad\x00id\ndata: second\n\n",
        [("message", "first", "cursor", None), ("message", "second", "cursor", None)],
        id="invalid-id-is-ignored",
    ),
    pytest.param(
        b": comment\nData: ignored\nunknown: ignored\n\ndata:  space\ndata:\ttab\ndata:x: y\n\n",
        [("message", " space\n\ttab\nx: y", "", None)],
        id="field-whitespace-and-comments",
    ),
    pytest.param(
        b"event: first\nevent: second\nretry: 1500\nretry: invalid\ndata: payload\n\ndata: next\n\n",
        [("second", "payload", "", 1500), ("message", "next", "", None)],
        id="field-state",
    ),
    # Preserve this low-level API's existing tolerance and metadata-only payloads.
    pytest.param(
        b"retry: +0010\nevent: update\n\nid: cursor\n\ndata: body\n\n",
        [
            ("update", "", "", 10),
            ("message", "", "cursor", None),
            ("message", "body", "cursor", None),
        ],
        id="permissive-metadata",
    ),
]


@asynccontextmanager
async def _retry_server(
    scenario: str,
    *,
    http2: bool = False,
) -> typing.AsyncGenerator[tuple[int, bytes, asyncio.Event, list[int]], None]:
    payload = b'{"delay": 0.001}' if scenario != "empty" else b""
    body = gzip.compress(payload) if scenario in ("gzip", "raw-gzip") else payload
    if scenario == "invalid-gzip":
        body = b"not a gzip stream"
    requests: list[int] = []
    writers: list[asyncio.StreamWriter] = []
    tasks: set[asyncio.Task[typing.Any]] = set()
    body_started = asyncio.Event()

    async def handle_http2(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:

        h2 = H2Connection(config=H2Configuration(client_side=False))
        h2.initiate_connection()
        writer.write(h2.data_to_send())
        while True:
            data = await reader.read(65536)
            if not data:
                return
            for event in h2.receive_data(data):
                if isinstance(event, RequestReceived):
                    requests.append(id(writer))
                    first = len(requests) == 1
                    data = body if first else b"ok"
                    headers = [
                        (b":status", b"503" if first else b"200"),
                        (b"content-length", str(len(data)).encode()),
                    ]
                    if first and scenario == "gzip":
                        headers.append((b"content-encoding", b"gzip"))
                    h2.send_headers(event.stream_id, headers)
                    h2.send_data(event.stream_id, data, end_stream=True)
            writer.write(h2.data_to_send())
            await writer.drain()

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks.add(task)
        writers.append(writer)
        try:
            if http2:
                await handle_http2(reader, writer)
                return
            while True:
                await reader.readuntil(b"\r\n\r\n")
                requests.append(id(writer))
                first = len(requests) == 1
                status = b"503 Service Unavailable" if first else b"200 OK"
                data = body if first else b"ok"
                headers = b"HTTP/1.1 " + status + b"\r\n"
                if first and scenario in ("gzip", "raw-gzip", "invalid-gzip"):
                    headers += b"Content-Encoding: gzip\r\n"
                if first and scenario in ("gzip", "raw-gzip"):
                    headers += b"Transfer-Encoding: chunked\r\n\r\n"
                    data = b"%x\r\n" % len(data) + data + b"\r\n0\r\n\r\n"
                else:
                    size = len(data) + (10 if first and scenario == "truncated" else 0)
                    headers += b"Content-Length: %d\r\n\r\n" % size
                writer.write(headers)
                await writer.drain()
                if first and scenario in ("stalled", "cancel"):
                    body_started.set()
                    await reader.read()
                    return
                writer.write(data)
                await writer.drain()
                if first and scenario == "truncated":
                    return
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            tasks.discard(task)

    context = None
    if http2:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(DEFAULT_CERTS["certfile"], DEFAULT_CERTS["keyfile"])
        context.set_alpn_protocols(["h2"])
    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    try:
        yield (
            server.sockets[0].getsockname()[1],
            body if scenario == "raw-gzip" else payload,
            body_started,
            requests,
        )
    finally:
        server.close()
        await server.wait_closed()
        for writer in writers:
            writer.close()
        if tasks:
            await asyncio.gather(*tasks)


@pytest.fixture(params=[False, True], ids=["tcp", "tls"])
def tls(request: pytest.FixtureRequest) -> bool:
    return bool(request.param)


@pytest.fixture(params=["wsproto", "fast"])
def websocket_connection(request: pytest.FixtureRequest) -> Any:
    """Run a synchronous WebSocket client while the peer uses the test loop."""
    from .websocket_helpers import _connection

    if request.param == "fast":
        pytest.importorskip("websockets", minversion="15.0")
    else:
        pytest.importorskip("wsproto")
    return functools.partial(_connection, False, implementation=request.param)


@pytest.fixture(params=["wsproto", "fast"])
def async_websocket_connection(request: pytest.FixtureRequest) -> Any:
    """Run an asynchronous WebSocket client and peer on the test loop."""
    from .websocket_helpers import _connection

    if request.param == "fast":
        pytest.importorskip("websockets", minversion="15.0")
    else:
        pytest.importorskip("wsproto")
    return functools.partial(_connection, True, implementation=request.param)


@pytest.fixture
def retry_server() -> Any:
    """Create a retry peer on the test loop, with bounded teardown."""
    return _retry_server
