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
            # Keep the port bound: it cannot be reused by another test.
            with pytest.raises(ConnectionRefusedError):
                await asyncio.wait_for(wasi_socket.create_connection(address), 2)
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


@pytest.mark.asyncio
@pytest.mark.usefixtures("python_udp_transport")
@pytest.mark.timeout(10)
class TestDatagram:
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
