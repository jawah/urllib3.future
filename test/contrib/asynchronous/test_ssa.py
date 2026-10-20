from __future__ import annotations

import asyncio
import errno
import socket
import ssl
import sys
import typing
from contextlib import asynccontextmanager
from unittest.mock import Mock

import pytest
import trustme

from urllib3._constant import UDP_LINUX_GRO
from urllib3.contrib.ssa import AsyncSocket, _gro
from urllib3.contrib.ssa._timeout import timeout
from urllib3.contrib.ssa._gro import (
    DatagramReader,
    DatagramWriter,
    _NativeOptimizedDatagramTransport,
    open_dgram_connection,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("tls", [False, True])
async def test_close_marks_tls_transport_closed_before_releasing_socket(
    tls: bool,
) -> None:
    sock = AsyncSocket()
    writer = Mock(spec=asyncio.StreamWriter)
    writer.get_extra_info.return_value = object() if tls else None
    sock._writer = writer
    original_socket = sock._sock

    def check_release() -> None:
        writer.close.assert_called_once()
        if tls:
            writer.transport.abort.assert_called_once()
        else:
            writer.transport.abort.assert_not_called()

    try:
        sock._sock = Mock(spec=socket.socket)
        sock._sock.close.side_effect = check_release
        sock.close()
        check_release()
        assert not sock._connect_called
        assert not sock._established.is_set()
    finally:
        original_socket.close()


@pytest.mark.skipif(sys.platform == "win32", reason="Requires POSIX descriptor reuse")
@pytest.mark.asyncio
async def test_tls_close_allows_immediate_descriptor_reuse() -> None:
    ca = trustme.CA()
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ca.issue_cert("localhost").configure_cert(server_context)
    client_context = ssl.create_default_context()
    ca.configure_trust(client_context)
    loop = asyncio.get_running_loop()
    transports: list[asyncio.BaseTransport] = []

    class Protocol(asyncio.Protocol):
        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            transports.append(transport)

    tls_server = await loop.create_server(Protocol, "127.0.0.1", 0, ssl=server_context)
    plain_server = await loop.create_server(Protocol, "127.0.0.1", 0)
    try:
        old = AsyncSocket(socket.AF_INET, socket.SOCK_STREAM)
        old.settimeout(2)
        replacement = None
        try:
            await old.connect(tls_server.sockets[0].getsockname())
            await old.wrap_socket(
                client_context, server_hostname="localhost", ssl_handshake_timeout=2
            )
            old_fd = old.fileno()
            old.close()
            replacement = AsyncSocket(socket.AF_INET, socket.SOCK_STREAM)
            replacement.settimeout(2)
            if replacement.fileno() != old_fd:
                pytest.skip(
                    "OS did not immediately reuse the closed fd; cannot deterministically reproduce the ownership race"
                )
            # Do not yield between close and reconnect: the old TLS transport
            # must release ownership before another socket reuses its fd.
            await replacement.connect(plain_server.sockets[0].getsockname())
            assert replacement._established.is_set()
        finally:
            if replacement is not None:
                replacement.close()
                await replacement.wait_for_close()
            old.close()
            await old.wait_for_close()
    finally:
        tls_server.close()
        plain_server.close()
        for transport in transports:
            transport.close()
        await tls_server.wait_closed()
        await plain_server.wait_closed()


@pytest.mark.asyncio
async def test_timeout_can_be_disabled_then_rescheduled() -> None:
    deadline = timeout(None)
    assert deadline.when() is None
    assert not deadline.expired()
    assert "created" in repr(deadline)
    with pytest.raises(RuntimeError, match="has not been entered"):
        deadline.reschedule(None)

    disabled_timer_survived = False
    with pytest.raises(TimeoutError):
        async with deadline:
            when = asyncio.get_running_loop().time() + 60
            deadline.reschedule(when)
            assert deadline.when() == when
            assert "active" in repr(deadline)
            # Even an already scheduled expiry can be withdrawn before it runs.
            deadline.reschedule(0)
            deadline.reschedule(None)
            await asyncio.sleep(0)
            assert not deadline.expired()
            assert deadline.when() is None
            disabled_timer_survived = True
            deadline.reschedule(0)
            await asyncio.sleep(0)

    assert disabled_timer_survived
    assert deadline.expired()
    assert "expired" in repr(deadline)
    with pytest.raises(RuntimeError, match="Cannot change state"):
        deadline.reschedule(None)
    with pytest.raises(RuntimeError, match="already been entered"):
        async with deadline:
            pytest.fail("An expired timeout cannot be entered again")


@pytest.mark.skipif(
    sys.platform == "wasi", reason="Tests the non-WASI compatibility facade"
)
@pytest.mark.asyncio
@pytest.mark.parametrize("listening", [False, True])
async def test_wasi_compat_create_connection(listening: bool) -> None:
    from urllib3.contrib.wasi._async import socket as wasi_socket

    finished = asyncio.Event()

    async def connected(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            writer.write(b"ready")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        address = listener.getsockname()
        if not listening:
            # A bound, non-listening socket can silently drop SYNs on macOS/Windows.
            listener.close()
            with pytest.raises(ConnectionRefusedError):
                # Windows have a MaxSynRetransmissions, we must set >2s. Otherwise it's going to be
                # regular TimeoutError.
                await asyncio.wait_for(wasi_socket.create_connection(address), 10)
            return

        server = await asyncio.start_server(connected, sock=listener)
        try:
            client = await asyncio.wait_for(wasi_socket.create_connection(address), 2)
            try:
                received = b""
                while len(received) < 5:
                    chunk = await asyncio.wait_for(client.recv(5 - len(received)), 2)
                    assert isinstance(chunk, bytes) and chunk
                    received += chunk
                assert received == b"ready"
                await asyncio.wait_for(finished.wait(), 2)
            finally:
                client.close()
                await client.wait_for_close()
        finally:
            server.close()
            await server.wait_closed()


@asynccontextmanager
async def datagram_pair(
    client_socket: socket.socket | None = None,
    *,
    server_socket: socket.socket | None = None,
) -> typing.AsyncGenerator[
    tuple[DatagramReader, DatagramWriter, DatagramReader, DatagramWriter], None
]:
    if server_socket is None:
        server_reader, server_writer = await open_dgram_connection(
            local_addr=("127.0.0.1", 0), family=socket.AF_INET
        )
    else:
        server_socket.setblocking(False)
        server_reader, server_writer = await open_dgram_connection(sock=server_socket)
    try:
        address = server_writer.get_extra_info("sockname")
        if client_socket is None:
            reader, writer = await open_dgram_connection(remote_addr=address)
        else:
            client_socket.setblocking(False)
            await asyncio.get_running_loop().sock_connect(client_socket, address)
            reader, writer = await open_dgram_connection(sock=client_socket)
        try:
            yield reader, writer, server_reader, server_writer
        finally:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), 2)
    finally:
        server_writer.close()
        await asyncio.wait_for(server_writer.wait_closed(), 2)


async def receive(reader: DatagramReader, count: int) -> list[bytes]:
    received: list[bytes] = []
    while len(received) < count:
        data = await asyncio.wait_for(reader.read(), 2)
        assert data, "Datagram transport closed before receiving all messages"
        received.extend(data if isinstance(data, list) else [data])
    return received


class ForwardingDatagramProtocol(asyncio.DatagramProtocol):
    """An application protocol that can react to errors while retaining stream I/O."""

    def __init__(
        self,
        delegate: asyncio.DatagramProtocol,
        on_error: typing.Callable[[Exception], None],
    ) -> None:
        self.delegate = delegate
        self.on_error = on_error

    def datagram_received(self, data: bytes, addr: typing.Any) -> None:
        self.delegate.datagram_received(data, addr)

    def error_received(self, exc: Exception) -> None:
        self.delegate.error_received(exc)
        self.on_error(exc)

    def connection_lost(self, exc: Exception | None) -> None:
        self.delegate.connection_lost(exc)

    def pause_writing(self) -> None:
        self.delegate.pause_writing()

    def resume_writing(self) -> None:
        self.delegate.resume_writing()


class RecordingDatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.received: asyncio.Queue[bytes] = asyncio.Queue()
        self.errors: asyncio.Queue[Exception] = asyncio.Queue()
        self.closed = asyncio.Event()

    def datagram_received(self, data: bytes, addr: typing.Any) -> None:
        self.received.put_nowait(data)

    def error_received(self, exc: Exception) -> None:
        self.errors.put_nowait(exc)

    def connection_lost(self, exc: Exception | None) -> None:
        self.closed.set()


@pytest.mark.asyncio
@pytest.mark.usefixtures("python_udp_transport")
@pytest.mark.timeout(10)
class TestDatagram:
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Proactor strands queued datagrams after a send error: https://github.com/python/cpython/issues/156698",
    )
    async def test_standard_transport_oversized_send_does_not_poison_reader(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Exercise the stdlib transport used on Windows, with real UDP sockets.
        monkeypatch.setattr(_gro, "_IS_LINUX", False)
        monkeypatch.setattr(_gro, "_IS_DARWIN", False)
        async with datagram_pair() as (reader, writer, peer_reader, peer_writer):
            writer.write(b"x" * 65536)
            writer.write(b"valid")
            assert await receive(peer_reader, 1) == [b"valid"]
            peer_writer.transport.sendto(b"reply", writer.get_extra_info("sockname"))
            assert await receive(reader, 1) == [b"reply"]

    @pytest.mark.skipif(sys.platform != "linux", reason="Linux UDP GRO/GSO")
    async def test_gro_preserves_datagrams_after_protocol_replacement(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            try:
                sock.setsockopt(socket.IPPROTO_UDP, UDP_LINUX_GRO, 1)
            except OSError:
                pytest.skip("Kernel does not support UDP_GRO")
            sock.bind(("127.0.0.1", 0))
            async with datagram_pair(server_socket=sock) as (
                _,
                writer,
                reader,
                peer_writer,
            ):
                if not typing.cast(
                    _NativeOptimizedDatagramTransport, writer.transport
                )._gso_enabled:
                    pytest.skip("Kernel does not support UDP_SEGMENT")
                peer_transport = peer_writer.transport
                original = typing.cast(
                    asyncio.DatagramProtocol, peer_transport.get_protocol()
                )
                errors: list[Exception] = []
                replacement = ForwardingDatagramProtocol(original, errors.append)
                messages = [b"a" * 1280, b"b" * 1280, b"short"]
                # A standard DatagramProtocol has no batch callback. Its replacement
                # must still receive each coalesced datagram with its original boundary.
                for protocol in (replacement, original):
                    peer_transport.set_protocol(protocol)
                    writer.writelines(messages)
                    assert await receive(reader, len(messages)) == messages
                assert errors == []

    @pytest.mark.skipif(sys.platform != "linux", reason="Linux UDP GSO")
    @pytest.mark.parametrize("grouped", [False, True])
    @pytest.mark.parametrize(
        "send_error,reaction",
        [
            (errno.EMSGSIZE, "continue"),
            (errno.ECONNREFUSED, "continue"),
            (errno.ECONNREFUSED, "close"),
            (errno.ECONNREFUSED, "abort"),
        ],
    )
    async def test_gso_fallback_send_error_preserves_other_datagrams(
        self, grouped: bool, send_error: int, reaction: str
    ) -> None:
        delivered: list[bytes] = []
        rejected = 1 if grouped else 0

        class RejectedSocket(socket.socket):
            sends = 0

            def sendmsg(self, *args: typing.Any, **kwargs: typing.Any) -> int:
                raise OSError(errno.EIO, "GSO rejected")

            def send(self, *args: typing.Any, **kwargs: typing.Any) -> int:
                self.sends += 1
                if self.sends == rejected + 1:
                    raise OSError(send_error, "individual datagram rejected")
                result = super().send(*args, **kwargs)
                delivered.append(args[0])
                return result

        with RejectedSocket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            async with datagram_pair(sock) as (reader, writer, peer, _):
                transport = typing.cast(
                    _NativeOptimizedDatagramTransport, writer.transport
                )
                if not transport._gso_enabled:
                    pytest.skip("Kernel does not support UDP_SEGMENT")
                errors: list[Exception] = []

                def react(exc: Exception) -> None:
                    errors.append(exc)
                    if reaction != "continue":
                        getattr(transport, reaction)()

                transport.set_protocol(
                    ForwardingDatagramProtocol(
                        typing.cast(asyncio.DatagramProtocol, transport.get_protocol()),
                        react,
                    )
                )
                messages = (
                    [b"aaaa", b"bbbb", b"cc", b"next group"]
                    if grouped
                    else [b"a", b"bbbb", b"cccc"]
                )
                writer.writelines(messages)
                if send_error != errno.EMSGSIZE:
                    with pytest.raises(ConnectionRefusedError):
                        await asyncio.wait_for(reader.read(), 2)
                    assert len(errors) == 1
                else:
                    assert errors == []
                expected = (
                    [data for i, data in enumerate(messages) if i != rejected]
                    if reaction == "continue"
                    else messages[:rejected]
                )
                if expected:
                    assert await receive(peer, len(expected)) == expected
                assert delivered == expected
                if reaction == "continue":
                    assert not writer.is_closing()
                    writer.write(b"after")
                    assert await receive(peer, 1) == [b"after"]
                else:
                    await asyncio.wait_for(writer.wait_closed(), 2)
                    assert await reader.read() == b""
                assert transport.get_write_buffer_size() == 0

    @pytest.mark.parametrize("deny_reuse", [False, True])
    async def test_endpoint_reuse_port_option_keeps_udp_usable(
        self, deny_reuse: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class ReuseSocket(socket.socket):
            def setsockopt(self, level: int, option: int, *args: typing.Any) -> None:
                if (
                    deny_reuse
                    and level == socket.SOL_SOCKET
                    and option == getattr(socket, "SO_REUSEPORT", None)
                ):
                    raise OSError(errno.ENOPROTOOPT, "reuse-port unavailable")
                super().setsockopt(level, option, *args)

        monkeypatch.setattr(socket, "socket", ReuseSocket)
        transport, protocol = await _gro.create_udp_endpoint(
            asyncio.get_running_loop(),
            RecordingDatagramProtocol,
            local_addr=("127.0.0.1", 0),
            family=socket.AF_INET,
            reuse_port=True,
        )
        assert isinstance(protocol, RecordingDatagramProtocol)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                sender.sendto(b"hello", transport.get_extra_info("sockname"))
            assert await asyncio.wait_for(protocol.received.get(), 2) == b"hello"
            assert protocol.errors.empty()
        finally:
            transport.close()
            await asyncio.wait_for(protocol.closed.wait(), 2)

    async def test_failed_endpoint_bind_closes_created_socket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        created: list[socket.socket] = []

        class Socket(socket.socket):
            def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
                super().__init__(*args, **kwargs)
                created.append(self)

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            monkeypatch.setattr(socket, "socket", Socket)
            with pytest.raises(OSError) as caught:
                await _gro.create_udp_endpoint(
                    asyncio.get_running_loop(),
                    RecordingDatagramProtocol,
                    local_addr=occupied.getsockname(),
                    family=socket.AF_INET,
                )
            assert caught.value.errno == errno.EADDRINUSE
        assert len(created) == 1 and created[0].fileno() == -1

    @pytest.mark.skipif(
        sys.platform not in ("linux", "darwin", "ios"),
        reason="The Python optimized transport requires a selector event loop",
    )
    async def test_endpoint_without_default_peer_reports_error_then_sends(self) -> None:
        transport, protocol = await _gro.create_udp_endpoint(
            asyncio.get_running_loop(), RecordingDatagramProtocol
        )
        assert isinstance(protocol, RecordingDatagramProtocol)
        try:
            transport.sendto(b"missing address")
            error = await asyncio.wait_for(protocol.errors.get(), 2)
            assert isinstance(error, OSError)
            assert not transport.is_closing()
            async with datagram_pair() as (_, _, peer, peer_writer):
                transport.sendto(b"valid", peer_writer.get_extra_info("sockname"))
                assert await receive(peer, 1) == [b"valid"]
        finally:
            transport.close()
            await asyncio.wait_for(protocol.closed.wait(), 2)

    @pytest.mark.skipif(
        sys.platform not in ("linux", "darwin", "ios"),
        reason="The Python optimized transport requires a selector event loop",
    )
    @pytest.mark.parametrize("waiting_reader", [False, True])
    @pytest.mark.parametrize(
        "first_error,retry_error",
        [
            (errno.EINTR, None),
            (errno.EINTR, errno.EAGAIN),
            (errno.EINTR, errno.EMSGSIZE),
            (errno.EINTR, errno.ECONNREFUSED),
            (errno.ECONNREFUSED, None),
        ],
    )
    async def test_immediate_send_error_preserves_next_datagram(
        self, first_error: int, retry_error: int | None, waiting_reader: bool
    ) -> None:
        class InterruptedSocket(socket.socket):
            sends = 0

            def send(self, *args: typing.Any, **kwargs: typing.Any) -> int:
                self.sends += 1
                failure = (
                    first_error
                    if self.sends == 1
                    else retry_error
                    if self.sends == 2
                    else None
                )
                if failure is not None:
                    raise OSError(failure, "send failed")
                return super().send(*args, **kwargs)

        with InterruptedSocket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            async with datagram_pair(sock) as (reader, writer, peer, peer_writer):
                transport = typing.cast(
                    _NativeOptimizedDatagramTransport, writer.transport
                )
                pending = (
                    asyncio.ensure_future(reader.read()) if waiting_reader else None
                )
                if pending is not None:
                    await asyncio.sleep(0)
                try:
                    writer.write(b"first")
                    writer.write(b"second")
                    await asyncio.wait_for(writer.drain(), 2)
                    dropped = (
                        errno.ECONNREFUSED in (first_error, retry_error)
                        or retry_error == errno.EMSGSIZE
                    )
                    assert await receive(peer, 1 if dropped else 2) == (
                        [b"second"] if dropped else [b"first", b"second"]
                    )
                    if errno.ECONNREFUSED in (first_error, retry_error):
                        with pytest.raises(ConnectionRefusedError):
                            await asyncio.wait_for(
                                pending if pending is not None else reader.read(), 2
                            )
                        pending = None
                        assert not reader.at_eof()
                    peer_writer.transport.sendto(b"reply", sock.getsockname())
                    assert (
                        await asyncio.wait_for(
                            pending if pending is not None else reader.read(), 2
                        )
                        == b"reply"
                    )
                    assert transport.get_write_buffer_size() == 0
                finally:
                    if pending is not None and not pending.done():
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)

    @pytest.mark.skipif(
        sys.platform not in ("linux", "darwin", "ios"),
        reason="The Python optimized transport requires a selector event loop",
    )
    @pytest.mark.parametrize("reaction", ["continue", "close", "abort"])
    async def test_queued_send_error_allows_protocol_reaction(
        self, reaction: str
    ) -> None:
        class BusySocket(socket.socket):
            sends = 0

            def send(self, *args: typing.Any, **kwargs: typing.Any) -> int:
                self.sends += 1
                if self.sends == 1:
                    raise BlockingIOError(errno.EAGAIN, "send buffer full")
                if self.sends == 2:
                    raise ConnectionRefusedError(
                        errno.ECONNREFUSED, "queued send failed"
                    )
                return super().send(*args, **kwargs)

        with BusySocket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            async with datagram_pair(sock) as (reader, writer, peer, _):
                transport = typing.cast(
                    _NativeOptimizedDatagramTransport, writer.transport
                )
                errors: list[Exception] = []

                def react(exc: Exception) -> None:
                    errors.append(exc)
                    if reaction != "continue":
                        getattr(transport, reaction)()

                protocol = ForwardingDatagramProtocol(
                    typing.cast(asyncio.DatagramProtocol, transport.get_protocol()),
                    react,
                )
                transport.set_protocol(protocol)
                assert transport.get_protocol() is protocol
                for data in (b"rejected", b"second", b"third"):
                    writer.write(data)
                with pytest.raises(ConnectionRefusedError):
                    await asyncio.wait_for(reader.read(), 2)
                if reaction == "continue":
                    assert await receive(peer, 2) == [b"second", b"third"]
                    assert not writer.is_closing()
                else:
                    await asyncio.wait_for(writer.wait_closed(), 2)
                    assert await reader.read() == b""
                    if reaction == "close":
                        assert await receive(peer, 2) == [b"second", b"third"]
                    else:
                        # The aborted suffix never reached send().
                        assert sock.sends == 2
                    transport.close()
                    transport.abort()
                    with pytest.raises(OSError, match="closing"):
                        transport.sendto(b"closed")
                    with pytest.raises(OSError, match="closing"):
                        transport.sendto_many([b"closed"])
                assert len(errors) == 1
                assert transport.get_write_buffer_size() == 0

    @pytest.mark.parametrize("body_type", [bytes, bytearray, memoryview, list])
    async def test_datagram_round_trip(self, body_type: typing.Any) -> None:
        async with datagram_pair() as (reader, writer, server_reader, server_writer):
            messages = [b"first message", b"second message"]
            if body_type is list:
                writer.write(messages)
            else:
                for message in messages:
                    writer.write(body_type(message))
            await writer.drain()
            assert await receive(server_reader, len(messages)) == messages

            server_writer.transport.sendto(b"reply", writer.get_extra_info("sockname"))
            assert await receive(reader, 1) == [b"reply"]

    async def test_datagram_cancelled_read_can_be_reused(self) -> None:
        async with datagram_pair() as (reader, writer, server_reader, server_writer):
            pending = asyncio.create_task(reader.read())
            await asyncio.sleep(0)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending

            writer.writelines([b"after cancellation"])
            assert await receive(server_reader, 1) == [b"after cancellation"]
            server_writer.transport.sendto(b"reply", writer.get_extra_info("sockname"))
            assert await receive(reader, 1) == [b"reply"]

    async def test_concurrent_read_does_not_displace_waiter(self) -> None:
        async with datagram_pair() as (reader, writer, _, server_writer):
            pending = asyncio.create_task(reader.read())
            await asyncio.sleep(0)
            try:
                with pytest.raises(RuntimeError, match="called concurrently"):
                    await reader.read()
                server_writer.transport.sendto(
                    b"reply", writer.get_extra_info("sockname")
                )
                assert await asyncio.wait_for(pending, 2) == b"reply"
            finally:
                if not pending.done():
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending

    @pytest.mark.parametrize("abort", [False, True])
    async def test_datagram_close_wakes_reader(self, abort: bool) -> None:
        async with datagram_pair() as (reader, writer, _, _):
            pending = asyncio.create_task(reader.read())
            await asyncio.sleep(0)
            if abort:
                writer.transport.abort()
            else:
                writer.close()
            assert writer.is_closing()
            assert await asyncio.wait_for(pending, 2) == b""
            await asyncio.wait_for(writer.wait_closed(), 2)
            assert reader.at_eof()
            assert await reader.read() == b""
            writer.write(b"already closed")
            await writer.drain()

    @pytest.mark.skipif(
        sys.platform not in ("linux", "darwin", "ios"),
        reason="The Python optimized transport requires a selector event loop",
    )
    async def test_datagram_pause_resume_reading(self) -> None:
        async with datagram_pair() as (reader, writer, server_reader, server_writer):
            transport = typing.cast(_NativeOptimizedDatagramTransport, writer.transport)
            transport.pause_reading()
            transport.pause_reading()
            writer.write(b"request")
            assert await receive(server_reader, 1) == [b"request"]
            server_writer.transport.sendto(b"reply", writer.get_extra_info("sockname"))
            transport.resume_reading()
            transport.resume_reading()
            assert await receive(reader, 1) == [b"reply"]

    @pytest.mark.skipif(sys.platform != "linux", reason="Linux UDP GSO")
    async def test_datagram_batch_preserves_message_boundaries(self) -> None:
        async with datagram_pair() as (_, writer, server_reader, _):
            # Exercise equal-sized segments, a short final segment, and a new group.
            messages = [b"a" * 1200, b"b" * 1200, b"c" * 100, b"d" * 1400]
            writer.writelines(messages)
            assert await receive(server_reader, len(messages)) == messages

    @pytest.mark.skipif(sys.platform != "linux", reason="Linux UDP GRO/GSO")
    async def test_gro_preserves_short_final_segment(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            try:
                sock.setsockopt(_gro._SOL_UDP, UDP_LINUX_GRO, 1)
            except OSError:
                pytest.skip("Kernel does not support UDP_GRO")
            sock.bind(("127.0.0.1", 0))
            async with datagram_pair(server_socket=sock) as (_, writer, reader, _):
                transport = typing.cast(
                    _NativeOptimizedDatagramTransport, writer.transport
                )
                if not transport._gso_enabled:
                    pytest.skip("Kernel does not support UDP_SEGMENT")
                messages = [b"a" * 1200, b"b" * 1200, b"c" * 100]
                writer.writelines(messages)
                assert await receive(reader, len(messages)) == messages

    @pytest.mark.skipif(sys.platform != "linux", reason="Linux UDP GSO")
    @pytest.mark.parametrize("gso_error", [errno.EIO, errno.EMSGSIZE])
    async def test_gso_fallback_resumes_at_the_unsent_duplicate(
        self, gso_error: int
    ) -> None:
        class RejectedGSOSocket(socket.socket):
            sends = 0

            def sendmsg(self, *args: typing.Any, **kwargs: typing.Any) -> int:
                # Model a NIC rejecting GSO; individual sends still use real UDP.
                raise OSError(gso_error, "GSO rejected")

            def send(self, *args: typing.Any, **kwargs: typing.Any) -> int:
                self.sends += 1
                if self.sends == 2:
                    raise BlockingIOError(errno.EAGAIN, "send buffer full")
                return super().send(*args, **kwargs)

        with RejectedGSOSocket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            async with datagram_pair(sock) as (_, writer, server_reader, _):
                transport = typing.cast(
                    _NativeOptimizedDatagramTransport, writer.transport
                )
                if not transport._gso_enabled:
                    pytest.skip("Kernel does not support UDP_SEGMENT")
                messages = [b"same", b"same", b"last", b"next group"]
                writer.writelines(messages)
                received: list[bytes] = []
                while not received or received[-1] != messages[-1]:
                    received.extend(await receive(server_reader, 1))
                assert received == messages
                assert transport._gso_enabled == (gso_error != errno.EIO)

    @pytest.mark.skipif(
        sys.platform not in ("linux", "darwin", "ios"),
        reason="The Python optimized transport requires a selector event loop",
    )
    @pytest.mark.parametrize("send_error", [errno.EAGAIN, errno.EINTR, errno.EMSGSIZE])
    async def test_queued_datagram_recovers_from_send_error(
        self, send_error: int
    ) -> None:
        class BusySocket(socket.socket):
            sends = 0

            def send(self, *args: typing.Any, **kwargs: typing.Any) -> int:
                self.sends += 1
                if self.sends == 1:
                    raise BlockingIOError(errno.EAGAIN, "send buffer full")
                if self.sends == 2:
                    raise OSError(send_error, "queued send failed")
                return super().send(*args, **kwargs)

        with BusySocket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            async with datagram_pair(sock) as (_, writer, reader, _):
                transport = typing.cast(
                    _NativeOptimizedDatagramTransport, writer.transport
                )
                messages = [b"first", b"second", b"last"]
                for message in messages:
                    writer.write(message)
                assert transport.get_write_buffer_size() == sum(map(len, messages))
                # Closing must still drain the queue, including after EAGAIN/EINTR.
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), 2)
                expected = messages[1:] if send_error == errno.EMSGSIZE else messages
                assert await receive(reader, len(expected)) == expected
                assert transport.get_write_buffer_size() == 0

    @pytest.mark.skipif(sys.platform != "linux", reason="Linux UDP GSO")
    @pytest.mark.parametrize("send_error", [errno.EAGAIN, errno.EINTR])
    async def test_gso_retry_preserves_remaining_groups(self, send_error: int) -> None:
        class BusyGSOSocket(socket.socket):
            batches = 0

            def sendmsg(self, *args: typing.Any, **kwargs: typing.Any) -> int:
                self.batches += 1
                if self.batches == 1:
                    raise OSError(send_error, "GSO send temporarily unavailable")
                return super().sendmsg(*args, **kwargs)

        with BusyGSOSocket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            async with datagram_pair(sock) as (_, writer, reader, _):
                transport = typing.cast(
                    _NativeOptimizedDatagramTransport, writer.transport
                )
                if not transport._gso_enabled:
                    pytest.skip("Kernel does not support UDP_SEGMENT")
                # The first group succeeds. The failing group contains duplicates;
                # its suffix and subsequent groups must be queued exactly once.
                messages = [b"a", b"same", b"same", b"tail", b"next group"]
                writer.writelines(messages)
                writer.writelines([b"after"])
                assert await receive(reader, len(messages) + 1) == messages + [b"after"]
                assert sock.batches == 1
                assert transport._gso_enabled
                assert transport.get_write_buffer_size() == 0

    @pytest.mark.skipif(
        sys.platform not in ("linux", "darwin", "ios"),
        reason="The Python optimized transport requires a selector event loop",
    )
    async def test_datagram_backpressure_drains_in_order(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class BusySocket(socket.socket):
            busy = True

            def send(self, *args: typing.Any, **kwargs: typing.Any) -> int:
                if self.busy:
                    self.busy = False
                    raise BlockingIOError(errno.EAGAIN, "send buffer full")
                return super().send(*args, **kwargs)

        # Reach flow control with a few packets instead of flooding the CI network.
        monkeypatch.setattr(_gro, "_HIGH_WATERMARK", 16)
        monkeypatch.setattr(_gro, "_LOW_WATERMARK", 8)
        with BusySocket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            async with datagram_pair(sock) as (_, writer, server_reader, _):
                transport = typing.cast(
                    _NativeOptimizedDatagramTransport, writer.transport
                )
                messages = [b"a" * 8, b"b" * 8, b"c" * 8, b"d" * 8]
                writer.write(messages[0])
                writer.write(messages[1])
                writer.writelines(messages[2:])
                assert transport.get_write_buffer_size() == 32
                await writer.drain()
                assert transport.get_write_buffer_size() == 0
                assert await receive(server_reader, len(messages)) == messages

    @pytest.mark.skipif(
        sys.platform not in ("linux", "darwin", "ios"),
        reason="The Python optimized transport requires a selector event loop",
    )
    @pytest.mark.parametrize("batch", [False, True])
    async def test_datagram_oversized_probe_does_not_lose_next_packet(
        self, batch: bool
    ) -> None:
        async with datagram_pair() as (_, writer, server_reader, _):
            messages = [b"x" * 65536, b"last"]
            if batch:
                writer.writelines(messages)
            else:
                for message in messages:
                    writer.write(message)
            assert await receive(server_reader, 1) == [b"last"]

    @pytest.mark.skipif(
        sys.platform not in ("linux", "darwin", "ios"),
        reason="The Python optimized transport requires recvmsg",
    )
    async def test_datagram_truncated_input_reports_error_and_recovers(self) -> None:
        async with datagram_pair() as (reader, writer, _, server_writer):
            address = writer.get_extra_info("sockname")
            payload = b"x" * 2000
            server_writer.transport.sendto(payload, address)
            with pytest.raises(OSError, match="recvmsg payload truncated"):
                await asyncio.wait_for(reader.read(), 2)
            # The first datagram was truncated, but the receive buffer grew.
            server_writer.transport.sendto(payload, address)
            assert await receive(reader, 1) == [payload]
