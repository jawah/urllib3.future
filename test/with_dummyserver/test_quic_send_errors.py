from __future__ import annotations

import asyncio
import errno
import inspect
import socket
import sys
import time
import typing
from pathlib import Path

import pytest
import trustme
from tornado.platform.asyncio import AsyncIOLoop

from dummyserver.server import run_loop_in_thread
from urllib3 import AsyncHTTPSConnectionPool, HTTPSConnectionPool, HttpVersion
from urllib3.backend.hface import _HAS_HTTP3_SUPPORT
from urllib3.contrib.webextensions.raw import RawExtensionFromHTTP
from urllib3.util.wait import wait_for_read


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


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize("packet_loss", [False, True])
async def test_flow_control_accepts_batched_quic_input(
    quic_echo_server: tuple[int, str, typing.Callable[..., None]],
    asynchronous: bool,
    raw: bool,
    packet_loss: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib3.backend import hface
    from urllib3.contrib.webextensions._async.raw import AsyncRawExtensionFromHTTP
    from urllib3.util.sub_timeout import AsyncSubTimeout, SubTimeout

    async def call(
        fn: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        result = fn(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    pool = (AsyncHTTPSConnectionPool if asynchronous else HTTPSConnectionPool)(
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
        assert (await call(pool.request, "GET", "/")).status == 200
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

        async def require_ack_wait() -> None:
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
                    await asyncio.sleep(0)
                    data = protocol.bytes_to_send()
                await call(sock.sendall, data)
                sent += 1
                assert time.monotonic() < deadline, "QUIC did not enter ACK wait"

        def batch(data: bytes | list[bytes]) -> bytes | list[bytes]:
            nonlocal batches
            if data:
                batches += 1
                return data if isinstance(data, list) else [data]
            return data

        target: typing.Any
        if not asynchronous and conn._dgram_gro_enabled:
            target, name = hface, "sync_recv_gro"
        else:
            target, name = type(sock), "recv"
        receive = getattr(target, name)

        async def receive_async(
            current: typing.Any, *args: typing.Any, **kwargs: typing.Any
        ) -> typing.Any:
            data = await receive(current, *args, **kwargs)
            return batch(data) if current is sock else data

        def receive_sync(
            current: typing.Any, *args: typing.Any, **kwargs: typing.Any
        ) -> typing.Any:
            data = receive(current, *args, **kwargs)
            return batch(data) if current is sock else data

        with monkeypatch.context() as transport:
            transport.setattr(
                target, name, receive_async if asynchronous else receive_sync
            )
            transport.setattr(
                protocol, "should_wait_remote_flow_control", observe_credit
            )
            transport.setattr(SubTimeout, "__exit__", observe_timeout)
            transport.setattr(AsyncSubTimeout, "__aexit__", observe_async_timeout)
            body = b"x" * 65536
            if raw:
                extension = (
                    AsyncRawExtensionFromHTTP()
                    if asynchronous
                    else RawExtensionFromHTTP()
                )
                response = await call(
                    pool.request,
                    "CONNECT",
                    "/",
                    preload_content=False,
                    headers={":protocol": "bytestream"},
                    extension=extension,
                )
                await require_ack_wait()
                for offset in range(0, len(body), 1024):
                    await call(extension.send_payload, body[offset : offset + 1024])
                received = bytearray()
                while len(received) < len(body):
                    chunk = await call(extension.next_payload)
                    assert chunk
                    received.extend(chunk)
                assert received == body
                await call(extension.close)
            else:
                await require_ack_wait()
                result = await call(
                    pool.request, "POST", "/", body=body, preload_content=False
                )
                assert await call(result.read) == body
        assert batches > 0 and flow_waits > 0
        if packet_loss:
            assert timer_firings > 0
        assert (await call(pool.request, "GET", "/next")).status == 200
    finally:
        quic_echo_server[2](pause_reads=False, hold_transmit=False)
        if response is not None:
            await call(response.close)
        await call(pool.close)


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
