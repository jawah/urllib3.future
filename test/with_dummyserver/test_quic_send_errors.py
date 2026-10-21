from __future__ import annotations

import asyncio
import errno
import socket
import time
import typing
from pathlib import Path

import pytest
import trustme
from tornado.platform.asyncio import AsyncIOLoop

from dummyserver.server import run_loop_in_thread
from urllib3 import HTTPSConnectionPool, HttpVersion
from urllib3.backend.hface import _HAS_HTTP3_SUPPORT
from urllib3.contrib.webextensions.raw import RawExtensionFromHTTP
from urllib3.util.wait import wait_for_read


@pytest.fixture
def quic_echo_server(
    tmp_path: Path,
) -> typing.Iterator[tuple[int, str, typing.Callable[[], None]]]:
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

            def peer_ping() -> None:
                async def send() -> None:
                    for peer in set(server._protocols.values()):
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
                    peer.close()
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*(peer.wait_closed() for peer in peers)), 5
                    )
                finally:
                    server.close()

            asyncio.run_coroutine_threadsafe(stop(), loop).result(10)


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
        conn = next(iter(pool.pool._registry.values()))
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
            return bytes_to_send()

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
            return waiting

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
