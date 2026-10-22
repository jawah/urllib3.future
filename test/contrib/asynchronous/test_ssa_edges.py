from __future__ import annotations

import asyncio
import errno
import socket
import ssl
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncIterator, cast

import pytest
import trustme

from urllib3._constant import UDP_LINUX_GRO, UDP_LINUX_SEGMENT
from urllib3.contrib import ssa
from urllib3.contrib.ssa import AsyncSocket, _gro
from .test_ssa import datagram_pair, receive, ForwardingDatagramProtocol


@asynccontextmanager
async def stream_pair(
    tls: bool = False,
    *,
    peer_may_abort: bool = False,
) -> AsyncIterator[tuple[AsyncSocket, ssl.SSLContext]]:
    ca = trustme.CA()
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ca.issue_cert("localhost").configure_cert(server_context)
    client_context = ssl.create_default_context()
    ca.configure_trust(client_context)
    handlers: list[asyncio.Task[Any]] = []

    async def connected(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        handlers.append(task)
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            if not peer_may_abort:
                raise
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                if not peer_may_abort:
                    raise

    server = await asyncio.start_server(
        connected, "127.0.0.1", 0, ssl=server_context if tls else None
    )
    sock = AsyncSocket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        await sock.connect(server.sockets[0].getsockname())
        yield sock, client_context
    finally:
        sock.close()
        await sock.wait_for_close()
        server.close()
        await server.wait_closed()
        if handlers:
            await asyncio.wait_for(asyncio.gather(*handlers), 5)


async def receive_stream(sock: AsyncSocket, size: int, first: bytes) -> bytes:
    data = bytearray(first)
    while len(data) < size:
        chunk = await sock.recv(size - len(data))
        assert isinstance(chunk, bytes) and chunk, "Peer closed before the full reply"
        data.extend(chunk)
    return bytes(data)


@pytest.mark.asyncio
async def test_unconnected_readiness_and_close_wait() -> None:
    sock = AsyncSocket()
    try:
        await sock.wait_for_close()
        with pytest.raises(OSError, match="connected stream"):
            sock.read_ready()
        with pytest.raises(OSError, match="connected stream"):
            await sock.until_data_available()
    finally:
        sock.close()
        await sock.wait_for_close()


@pytest.mark.asyncio
async def test_connected_socket_aliases_and_reader_error() -> None:
    async with stream_pair() as (sock, _):
        # Waiting for closure must not block while the socket is still connected.
        await asyncio.wait_for(sock.wait_for_close(), 5)
        with pytest.raises(OSError, match="connect twice"):
            await sock.connect(sock.getpeername())
        await sock.send(b"hello")
        first = await sock.read()
        assert isinstance(first, bytes)
        assert await receive_stream(sock, 5, first) == b"hello"
        error = ConnectionResetError("injected receive failure")
        assert isinstance(sock._reader, asyncio.StreamReader)
        sock._reader.set_exception(error)
        with pytest.raises(ConnectionResetError) as raised:
            await sock.until_data_available()
        assert raised.value is error


@pytest.mark.asyncio
async def test_windows_hostname_normalization_uses_system_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Run the Windows hostname branch while keeping real socket and DNS I/O.
    monkeypatch.setattr(ssa, "platform", SimpleNamespace(system=lambda: "Windows"))
    async with stream_pair() as (sock, _):
        other = AsyncSocket(socket.AF_INET, socket.SOCK_STREAM)
        other.settimeout(5)
        try:
            await other.connect(("localhost", sock.getpeername()[1]))
            assert other.getpeername()[0] == "127.0.0.1"
            await other.sendall(b"resolved")
            first = await other.recv()
            assert isinstance(first, bytes)
            assert await receive_stream(other, 8, first) == b"resolved"
        finally:
            other.close()
            await other.wait_for_close()


@pytest.mark.asyncio
async def test_read_waits_for_tls_upgrade_to_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    entered, resume = asyncio.Event(), asyncio.Event()
    start_tls = loop.start_tls

    async def delayed_start_tls(*args: Any, **kwargs: Any) -> Any:
        transport = await start_tls(*args, **kwargs)
        entered.set()
        await resume.wait()
        return transport

    async with stream_pair(tls=True) as (sock, ctx):
        with monkeypatch.context() as patch:
            patch.setattr(loop, "start_tls", delayed_start_tls)
            upgrade = asyncio.create_task(
                sock.wrap_socket(ctx, server_hostname="localhost")
            )
            reader = None
            try:
                await asyncio.wait_for(entered.wait(), 5)
                reading = asyncio.Event()
                established = sock._established.wait

                async def read_waiting_for_tls() -> None:
                    reading.set()
                    await established()

                patch.setattr(sock._established, "wait", read_waiting_for_tls)
                reader = asyncio.create_task(sock.recv())
                await asyncio.wait_for(reading.wait(), 5)
                assert not reader.done()
                resume.set()
                secured = await asyncio.wait_for(upgrade, 5)
                assert secured._sslobj is secured.sslobj
                assert secured.cipher() == secured.sslobj.cipher()
                assert secured.cipher() is not None
                await secured.sendall(b"secure")
                first = await asyncio.wait_for(reader, 5)
                assert isinstance(first, bytes)
                assert await receive_stream(secured, 6, first) == b"secure"
            finally:
                resume.set()
                upgrade.cancel()
                if reader is not None:
                    reader.cancel()
                await asyncio.gather(
                    upgrade,
                    *([reader] if reader is not None else []),
                    return_exceptions=True,
                )


@pytest.mark.asyncio
async def test_tls_transport_disappearing_after_handshake_reports_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    start_tls = loop.start_tls

    async def lost_transport(*args: Any, **kwargs: Any) -> None:
        transport = await start_tls(*args, **kwargs)
        assert transport is not None
        transport.close()
        return None

    async with stream_pair(tls=True, peer_may_abort=True) as (sock, ctx):
        monkeypatch.setattr(loop, "start_tls", lost_transport)
        with pytest.raises(OSError, match="failed silently during TLS handshake"):
            await sock.wrap_socket(ctx, server_hostname="localhost")


@pytest.mark.skipif(not hasattr(socket.socket, "sendmsg"), reason="POSIX sendmsg")
@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize("failure", [errno.EMSGSIZE, errno.ECONNREFUSED])
def test_sync_gso_send_failure_preserves_datagrams(grouped: bool, failure: int) -> None:
    class Sender(socket.socket):
        failed = False

        def sendmsg(self, buffers: Any, *args: Any, **kwargs: Any) -> int:
            # Force the batch fallback while individual sends remain real UDP.
            raise OSError(errno.EMSGSIZE, "injected rejected batch")

        def send(self, data: Any, *args: Any, **kwargs: Any) -> int:
            if not self.failed:
                self.failed = True
                raise OSError(failure, "injected datagram failure")
            return super().send(data, *args, **kwargs)

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver, Sender(
        socket.AF_INET, socket.SOCK_DGRAM
    ) as sender:
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(5)
        sender.connect(receiver.getsockname())
        messages = [b"one", b"two"] if grouped else [b"one"]
        if failure == errno.ECONNREFUSED:
            with pytest.raises(ConnectionRefusedError):
                _gro.sync_sendmsg_gso(sender, messages)
        else:
            _gro.sync_sendmsg_gso(sender, messages)
            if grouped:
                assert receiver.recv(1024) == b"two"
        _gro.sync_sendmsg_gso(sender, [])
        _gro.sync_sendmsg_gso(sender, [b"after"])
        assert receiver.recv(1024) == b"after"


@pytest.mark.asyncio
@pytest.mark.usefixtures("python_udp_transport")
async def test_empty_udp_batch_and_close_during_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform not in ("linux", "darwin", "ios"):
        pytest.skip("Python recvmsg transport")
    monkeypatch.setattr(_gro, "_sock_has_gso", lambda sock: False)
    async with datagram_pair() as (reader, writer, peer, _):
        transport = cast(_gro._NativeOptimizedDatagramTransport, writer.transport)
        writer.writelines([])
        original = cast(asyncio.DatagramProtocol, transport.get_protocol())
        errors: list[Exception] = []

        def close_on_error(exc: Exception) -> None:
            errors.append(exc)
            transport.close()

        transport.set_protocol(ForwardingDatagramProtocol(original, close_on_error))
        original_send = type(transport._sock).send
        with monkeypatch.context() as io:

            def rejected(current: Any, data: Any, *args: Any, **kwargs: Any) -> Any:
                if current is transport._sock:
                    raise ConnectionRefusedError(
                        errno.ECONNREFUSED, "injected peer rejection"
                    )
                return original_send(current, data, *args, **kwargs)

            io.setattr(type(transport._sock), "send", rejected)
            writer.writelines([b"first", b"must not be sent"])
        assert len(errors) == 1
        await asyncio.wait_for(writer.wait_closed(), 5)
        # A queued readiness callback is harmless after closing the descriptor.
        transport._on_readable()
        with pytest.raises(ConnectionRefusedError):
            await reader.read()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux UDP GRO metadata")
@pytest.mark.parametrize("mode", ["truncated", "ancillary-truncated", "single"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
@pytest.mark.usefixtures("python_udp_transport")
async def test_kernel_gro_receive_boundaries(mode: str, asynchronous: bool) -> None:
    class Receiver(socket.socket):
        saw_control = False

        def recvmsg(
            self, bufsize: int, ancbufsize: int = 0, *args: Any, **kwargs: Any
        ) -> Any:
            data, ancillary, flags, address = super().recvmsg(
                1 if mode == "truncated" else bufsize,
                0 if mode == "ancillary-truncated" else ancbufsize,
                *args,
                **kwargs,
            )
            self.saw_control = bool(ancillary or flags)
            return data, ancillary, flags, address

    with Receiver(socket.AF_INET, socket.SOCK_DGRAM) as receiver, socket.socket(
        socket.AF_INET, socket.SOCK_DGRAM
    ) as sender:
        try:
            receiver.setsockopt(_gro._SOL_UDP, UDP_LINUX_GRO, 1)
            sender.setsockopt(_gro._SOL_UDP, UDP_LINUX_SEGMENT, 0)
        except OSError:
            pytest.skip("Kernel does not support UDP segmentation offload")
        receiver.bind(("127.0.0.1", 0))
        sender.connect(receiver.getsockname())
        payload = b"segment"
        # Two segments force ancillary truncation when recvmsg is given no
        # ancillary buffer space. A single segment may have no GRO metadata.
        packet = payload * (2 if mode == "ancillary-truncated" else 1)
        if asynchronous:
            receiver.setblocking(False)
            reader, writer = await _gro.open_dgram_connection(sock=receiver)
            try:
                sender.sendmsg(
                    [packet],
                    [
                        (
                            _gro._SOL_UDP,
                            UDP_LINUX_SEGMENT,
                            _gro._UINT16.pack(len(payload)),
                        )
                    ],
                )
                if mode == "truncated":
                    with pytest.raises(OSError, match="payload truncated"):
                        await asyncio.wait_for(reader.read(), 5)
                else:
                    assert await asyncio.wait_for(reader.read(), 5) == packet
            finally:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), 5)
        else:
            receiver.settimeout(5)
            sender.sendmsg(
                [packet],
                [
                    (
                        _gro._SOL_UDP,
                        UDP_LINUX_SEGMENT,
                        _gro._UINT16.pack(len(payload)),
                    )
                ],
            )
            if mode == "truncated":
                with pytest.raises(OSError, match="payload truncated"):
                    _gro.sync_recv_gro(receiver, 1024)
            else:
                assert _gro.sync_recv_gro(receiver, 1024) == packet
        if mode != "single":
            assert receiver.saw_control, "Kernel did not deliver truncation flags"


@pytest.mark.skipif(not hasattr(socket.socket, "sendmsg"), reason="POSIX sendmsg")
def test_sync_gso_empty_datagram_is_delivered() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender, socket.socket(
        socket.AF_INET, socket.SOCK_DGRAM
    ) as receiver:
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(5)
        sender.connect(receiver.getsockname())
        _gro.sync_sendmsg_gso(sender, [b""])
        assert receiver.recv(1) == b""


@pytest.mark.skipif(
    sys.platform not in ("linux", "darwin", "ios"),
    reason="Selector-based UDP transport",
)
@pytest.mark.asyncio
@pytest.mark.usefixtures("python_udp_transport")
@pytest.mark.parametrize(
    "operation", ["add_reader", "remove_reader", "add_writer", "remove_writer"]
)
async def test_selector_registration_failure_keeps_transport_closable(
    operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with datagram_pair() as (_, writer, peer, _):
        transport = cast(_gro._NativeOptimizedDatagramTransport, writer.transport)
        loop = asyncio.get_running_loop()
        writer.write(b"before")
        assert await receive(peer, 1) == [b"before"]
        transport.pause_reading()
        method = getattr(loop, operation)
        attempted = False

        def reject(fd: int, *args: Any) -> Any:
            nonlocal attempted
            if fd == transport._sock_fd:
                attempted = True
                # A failing remove must still release the real selector entry.
                if operation.startswith("remove"):
                    method(fd)
                raise OSError(errno.EBADF, "injected selector failure")
            return method(fd, *args)

        if operation == "remove_reader":
            transport.resume_reading()
        elif operation == "remove_writer":
            transport._register_writer()
        with monkeypatch.context() as patch:
            patch.setattr(loop, operation, reject)
            if operation == "add_reader":
                transport.resume_reading()
            elif operation == "remove_reader":
                transport.pause_reading()
            elif operation == "add_writer":
                transport._register_writer()
            else:
                transport._unregister_writer()
        assert attempted
        transport._register_reader()
        transport._register_reader()
        transport._register_writer()
        transport._register_writer()
        transport._unregister_writer()
        writer.write(b"after")
        assert await receive(peer, 1) == [b"after"]
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), 5)
        transport._register_reader()
        transport._register_writer()
        assert not transport._reader_registered and not transport._writer_registered


@pytest.mark.asyncio
@pytest.mark.usefixtures("python_udp_transport")
async def test_datagram_connection_loss_preserves_error_and_rejects_late_delivery() -> (
    None
):
    async with datagram_pair() as (reader, writer, peer, peer_writer):
        writer.write(b"already sent")
        packet = (await receive(peer, 1))[0]
        protocol = cast(_gro._DatagramBridgeProtocol, writer.transport.get_protocol())
        error = ConnectionResetError("injected connection loss")
        # Exercise the public DatagramProtocol notification used by asyncio.
        protocol.connection_lost(error)
        protocol.datagram_received(packet, peer_writer.get_extra_info("sockname"))
        protocol.datagrams_received([packet], peer_writer.get_extra_info("sockname"))
        protocol.datagrams_received([], peer_writer.get_extra_info("sockname"))
        with pytest.raises(ConnectionResetError) as raised:
            await reader.read()
        assert raised.value is error
        assert not reader._buffer
