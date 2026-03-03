"""
High-performance asyncio DatagramTransport with Linux-specific UDP
receive/send coalescing:

  - GRO (receive): ``setsockopt(SOL_UDP, UDP_GRO)`` + ``recvmsg`` cmsg
  - GSO (send):    ``sendmsg`` with ``UDP_SEGMENT`` cmsg

All other platforms fall back to the standard asyncio DatagramTransport.
"""

from __future__ import annotations

import asyncio
import collections
import socket
import struct
from collections import deque
from typing import Any, Callable

from ..._constant import UDP_LINUX_GRO, UDP_LINUX_SEGMENT

_UINT16 = struct.Struct("=H")

_DEFAULT_GRO_BUF = 65535

# Flow control watermarks for the custom write queue
_HIGH_WATERMARK = 64 * 1024
_LOW_WATERMARK = 16 * 1024

# GSO kernel limit: max segments per sendmsg call
_GSO_MAX_SEGMENTS = 64


def _sock_has_gro(sock: socket.socket) -> bool:
    """Check if GRO is enabled on *sock* (caller must have set it)."""
    try:
        return sock.getsockopt(socket.SOL_UDP, UDP_LINUX_GRO) == 1
    except OSError:
        return False


def _sock_has_gso(sock: socket.socket) -> bool:
    """Check if the kernel supports GSO on *sock*."""
    try:
        sock.getsockopt(socket.SOL_UDP, UDP_LINUX_SEGMENT)
        return True
    except OSError:
        return False


def _split_gro_buffer(buf: bytes, segment_size: int) -> list[bytes]:
    if segment_size <= 0 or len(buf) <= segment_size:
        return [buf]
    segments = []
    mv = memoryview(buf)
    for offset in range(0, len(buf), segment_size):
        segments.append(bytes(mv[offset : offset + segment_size]))
    return segments


def _group_by_segment_size(datagrams: list[bytes]) -> list[tuple[int, list[bytes]]]:
    """Group consecutive same-size datagrams for Linux UDP GSO.

    GSO requires all segments to be the same size (except the last,
    which may be shorter).  Max 64 segments per ``sendmsg`` call."""
    if not datagrams:
        return []
    groups: list[tuple[int, list[bytes]]] = []
    current_size = len(datagrams[0])
    current_group: list[bytes] = [datagrams[0]]
    for dgram in datagrams[1:]:
        if len(dgram) == current_size and len(current_group) < _GSO_MAX_SEGMENTS:
            current_group.append(dgram)
        else:
            groups.append((current_size, current_group))
            current_size = len(dgram)
            current_group = [dgram]
    groups.append((current_size, current_group))
    return groups


def sync_recv_gro(
    sock: socket.socket, bufsize: int, gro_segment_size: int = 1280
) -> bytes | list[bytes]:
    """Blocking recvmsg with GRO cmsg parsing. Returns bytes or list[bytes]."""
    ancbufsize = socket.CMSG_SPACE(_UINT16.size)

    data, ancdata, _flags, addr = sock.recvmsg(bufsize, ancbufsize)

    if not data:
        return b""

    segment_size = gro_segment_size

    for cmsg_level, cmsg_type, cmsg_data in ancdata:
        if cmsg_level == socket.SOL_UDP and cmsg_type == UDP_LINUX_GRO:
            (segment_size,) = _UINT16.unpack(cmsg_data[:2])
            break

    if len(data) <= segment_size:
        return data

    return _split_gro_buffer(data, segment_size)


def sync_sendmsg_gso(sock: socket.socket, datagrams: list[bytes]) -> None:
    """Batch-send datagrams using GSO. Falls back to individual sends."""
    for segment_size, group in _group_by_segment_size(datagrams):
        if len(group) == 1:
            sock.sendall(group[0])
            continue
        buf = b"".join(group)
        sock.sendmsg(
            [buf],
            [(socket.SOL_UDP, UDP_LINUX_SEGMENT, _UINT16.pack(segment_size))],
        )


class _OptimizedDatagramTransport(asyncio.DatagramTransport):
    __slots__ = (
        "_loop",
        "_sock",
        "_protocol",
        "_address",
        "_gro_enabled",
        "_gso_enabled",
        "_gro_segment_size",
        "_recv_buf_size",
        "_closing",
        "_closed_fut",
        "_extra",
        "_paused",
        "_write_ready",
        "_send_queue",
        "_buffer_size",
        "_protocol_paused",
    )

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        sock: socket.socket,
        protocol: asyncio.DatagramProtocol,
        address: tuple[str, int] | None,
        gro_enabled: bool,
        gso_enabled: bool,
        gro_segment_size: int,
    ) -> None:
        super().__init__()
        self._loop = loop
        self._sock = sock
        self._protocol = protocol
        self._address = address
        self._gro_enabled = gro_enabled
        self._gso_enabled = gso_enabled
        self._gro_segment_size = gro_segment_size
        self._closing = False
        self._closed_fut: asyncio.Future[None] = loop.create_future()
        self._paused = False
        self._write_ready = True

        # Write buffer state
        self._send_queue: deque[tuple[bytes, tuple[str, int] | None]] = (
            collections.deque()
        )
        self._buffer_size = 0
        self._protocol_paused = False

        self._recv_buf_size = _DEFAULT_GRO_BUF if gro_enabled else gro_segment_size

        self._extra = {
            "peername": address,
            "socket": sock,
            "sockname": sock.getsockname(),
        }

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return self._extra.get(name, default)

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._loop.remove_reader(self._sock.fileno())
        # Drain the write queue gracefully in the background
        if not self._send_queue:
            self._loop.call_soon(self._call_connection_lost, None)

    def abort(self) -> None:
        self._closing = True
        self._call_connection_lost(None)

    def _call_connection_lost(self, exc: Exception | None) -> None:
        try:
            self._loop.remove_reader(self._sock.fileno())
            self._loop.remove_writer(self._sock.fileno())
        except Exception:
            pass
        try:
            self._protocol.connection_lost(exc)
        finally:
            self._sock.close()
            if not self._closed_fut.done():
                self._closed_fut.set_result(None)

    def sendto(self, data: bytes, addr: tuple[str, int] | None = None) -> None:  # type: ignore[override]
        if self._closing:
            raise OSError("Transport is closing")

        target = addr or self._address
        if not self._write_ready:
            self._queue_write(data, target)
            return

        try:
            if target is not None:
                self._sock.sendto(data, target)
            else:
                self._sock.send(data)
        except BlockingIOError:
            self._write_ready = False
            self._loop.add_writer(self._sock.fileno(), self._on_write_ready)
            self._queue_write(data, target)
        except OSError as exc:
            self._protocol.error_received(exc)

    def sendto_many(self, datagrams: list[bytes]) -> None:
        """Send multiple datagrams, using GSO when available.

        Falls back to individual ``sendto`` calls when GSO is not
        supported or the socket write buffer is full."""
        if self._closing:
            raise OSError("Transport is closing")

        if not self._write_ready:
            target = self._address
            for dgram in datagrams:
                self._queue_write(dgram, target)
            return

        if self._gso_enabled:
            self._send_linux_gso(datagrams)
        else:
            for dgram in datagrams:
                self.sendto(dgram)

    def _send_linux_gso(self, datagrams: list[bytes]) -> None:
        for segment_size, group in _group_by_segment_size(datagrams):
            if len(group) == 1:
                # Single datagram — plain send (GSO needs >1 segment)
                try:
                    self._sock.send(group[0])
                except BlockingIOError:
                    self._write_ready = False
                    self._loop.add_writer(self._sock.fileno(), self._on_write_ready)
                    self._queue_write(group[0], self._address)
                    return
                except OSError as exc:
                    self._protocol.error_received(exc)
                continue

            buf = b"".join(group)
            try:
                self._sock.sendmsg(
                    [buf],
                    [(socket.SOL_UDP, UDP_LINUX_SEGMENT, _UINT16.pack(segment_size))],
                )
            except BlockingIOError:
                self._write_ready = False
                self._loop.add_writer(self._sock.fileno(), self._on_write_ready)
                # Queue individual datagrams as fallback
                for dgram in group:
                    self._queue_write(dgram, self._address)
                return
            except OSError as exc:
                self._protocol.error_received(exc)

    def _queue_write(self, data: bytes, addr: tuple[str, int] | None) -> None:
        self._send_queue.append((data, addr))
        self._buffer_size += len(data)
        self._maybe_pause_protocol()

    def _maybe_pause_protocol(self) -> None:
        if self._buffer_size >= _HIGH_WATERMARK and not self._protocol_paused:
            self._protocol_paused = True
            try:
                self._protocol.pause_writing()
            except AttributeError:
                pass

    def _maybe_resume_protocol(self) -> None:
        if self._protocol_paused and self._buffer_size <= _LOW_WATERMARK:
            self._protocol_paused = False
            try:
                self._protocol.resume_writing()
            except AttributeError:
                pass

    def _on_write_ready(self) -> None:
        while self._send_queue:
            data, addr = self._send_queue[0]
            try:
                if addr is not None:
                    self._sock.sendto(data, addr)
                else:
                    self._sock.send(data)
            except BlockingIOError:
                return
            except OSError as exc:
                self._protocol.error_received(exc)

            self._send_queue.popleft()
            self._buffer_size -= len(data)

        self._maybe_resume_protocol()
        self._write_ready = True
        self._loop.remove_writer(self._sock.fileno())

        if self._closing:
            self._call_connection_lost(None)

    def pause_reading(self) -> None:
        if not self._paused:
            self._paused = True
            self._loop.remove_reader(self._sock.fileno())

    def resume_reading(self) -> None:
        if self._paused:
            self._paused = False
            self._loop.add_reader(self._sock.fileno(), self._on_readable)

    def _start(self) -> None:
        self._loop.call_soon(self._protocol.connection_made, self)
        self._loop.add_reader(self._sock.fileno(), self._on_readable)

    def _on_readable(self) -> None:
        if self._closing:
            return
        self._recv_linux_gro()

    def _recv_linux_gro(self) -> None:
        ancbufsize = socket.CMSG_SPACE(_UINT16.size)
        while True:
            try:
                data, ancdata, _flags, addr = self._sock.recvmsg(
                    self._recv_buf_size, ancbufsize
                )
            except BlockingIOError:
                return
            except OSError as exc:
                self._protocol.error_received(exc)
                return

            if not data:
                return

            segment_size = self._gro_segment_size
            for cmsg_level, cmsg_type, cmsg_data in ancdata:
                if cmsg_level == socket.SOL_UDP and cmsg_type == UDP_LINUX_GRO:
                    (segment_size,) = _UINT16.unpack(cmsg_data[:2])
                    break

            if len(data) <= segment_size:
                self._protocol.datagram_received(data, addr)
            else:
                segments = _split_gro_buffer(data, segment_size)
                self._protocol.datagrams_received(segments, addr)  # type: ignore[attr-defined]


async def create_udp_endpoint(
    loop: asyncio.AbstractEventLoop,
    protocol_factory: Callable[[], asyncio.DatagramProtocol],
    *,
    local_addr: tuple[str, int] | None = None,
    remote_addr: tuple[str, int] | None = None,
    family: int = socket.AF_UNSPEC,
    reuse_port: bool = False,
    gro_segment_size: int = 1280,
    sock: socket.socket | None = None,
) -> tuple[asyncio.DatagramTransport, asyncio.DatagramProtocol]:
    if sock is not None:
        # Caller provided a pre-connected socket — skip creation/bind/connect.
        try:
            connected_addr = sock.getpeername()
        except OSError:
            connected_addr = None
    else:
        # 1. Resolve Addresses
        if family == socket.AF_UNSPEC:
            target_addr = local_addr or remote_addr
            if target_addr:
                infos = await loop.getaddrinfo(
                    target_addr[0], target_addr[1], type=socket.SOCK_DGRAM
                )
                family = infos[0][0]
            else:
                family = socket.AF_INET

        # 2. Create Socket
        sock = socket.socket(family, socket.SOCK_DGRAM)
        sock.setblocking(False)

        if reuse_port:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        if local_addr:
            sock.bind(local_addr)

        connected_addr = None

        if remote_addr:
            await loop.sock_connect(sock, remote_addr)
            connected_addr = remote_addr

    # 3. Determine capabilities — the caller is responsible for
    #    enabling GRO via setsockopt before handing us the socket.
    gro_enabled = _sock_has_gro(sock)
    gso_enabled = _sock_has_gso(sock)

    if not gro_enabled and not gso_enabled:
        return await loop.create_datagram_endpoint(
            lambda: protocol_factory(), sock=sock
        )

    # 4. Wire up optimized transport
    protocol = protocol_factory()

    transport = _OptimizedDatagramTransport(
        loop=loop,
        sock=sock,
        protocol=protocol,
        address=connected_addr,
        gro_enabled=gro_enabled,
        gso_enabled=gso_enabled,
        gro_segment_size=gro_segment_size,
    )

    transport._start()

    return transport, protocol


class DatagramReader:
    """API-compatible with ``asyncio.StreamReader`` (duck-typed) so that
    ``AsyncSocket`` can assign an instance to ``self._reader`` and the
    existing ``recv()`` code works unchanged.

    When GRO delivers multiple coalesced segments in a single syscall,
    ``feed_datagrams()`` stores them as a single ``list[bytes]`` entry.
    ``read()`` then returns that list directly so the caller can feed
    all segments to the QUIC state-machine in one pass before probing —
    avoiding the per-datagram recv→feed→probe round-trip overhead."""

    def __init__(self) -> None:
        self._buffer: deque[bytes | list[bytes]] = collections.deque()
        self._waiter: asyncio.Future[None] | None = None
        self._exception: BaseException | None = None
        self._eof = False

    def feed_datagram(self, data: bytes, addr: Any) -> None:
        """Feed a single (non-coalesced) datagram."""
        self._buffer.append(data)
        self._wake_waiter()

    def feed_datagrams(self, data: list[bytes], addr: Any) -> None:
        """Feed a batch of coalesced datagrams as a single entry."""
        self._buffer.append(data)
        self._wake_waiter()

    def set_exception(self, exc: BaseException) -> None:
        self._exception = exc
        self._wake_waiter()

    def connection_lost(self, exc: BaseException | None) -> None:
        self._eof = True
        if exc is not None:
            self._exception = exc
        self._wake_waiter()

    def _wake_waiter(self) -> None:
        waiter = self._waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    async def read(self, n: int = -1) -> bytes | list[bytes]:
        """Return the next entry from the buffer.

        * ``bytes``        — a single datagram (non-coalesced).
        * ``list[bytes]``  — a batch of coalesced datagrams from one
          GRO syscall.
        * ``b""``          — EOF.
        """
        if self._buffer:
            return self._buffer.popleft()

        if self._exception is not None:
            raise self._exception

        if self._eof:
            return b""

        self._waiter = asyncio.get_running_loop().create_future()
        try:
            await self._waiter
        finally:
            self._waiter = None

        if self._buffer:
            return self._buffer.popleft()

        if self._exception is not None:
            raise self._exception

        return b""


class DatagramWriter:
    """API-compatible with ``asyncio.StreamWriter`` (duck-typed) so that
    ``AsyncSocket`` can assign an instance to ``self._writer`` and the
    existing ``sendall()``, ``close()``, ``wait_for_close()`` code works
    unchanged."""

    def __init__(
        self,
        transport: asyncio.DatagramTransport,
    ) -> None:
        self._transport = transport
        self._closed_event = asyncio.Event()
        self._paused = False
        self._drain_waiter: asyncio.Future[None] | None = None

    @property
    def transport(self) -> asyncio.DatagramTransport:
        return self._transport

    def write(self, data: bytes | bytearray | memoryview | list[bytes]) -> None:
        if self._transport.is_closing():
            raise OSError("transport is closing")
        if isinstance(data, list):
            if hasattr(self._transport, "sendto_many"):
                self._transport.sendto_many(data)
            else:
                # Plain asyncio transport — send individually
                for dgram in data:
                    self._transport.sendto(dgram)
        else:
            self._transport.sendto(bytes(data))

    async def drain(self) -> None:
        if not self._paused:
            return
        self._drain_waiter = asyncio.get_running_loop().create_future()
        try:
            await self._drain_waiter
        finally:
            self._drain_waiter = None

    def close(self) -> None:
        self._transport.close()

    async def wait_closed(self) -> None:
        await self._closed_event.wait()

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return self._transport.get_extra_info(name, default)

    def _pause_writing(self) -> None:
        self._paused = True

    def _resume_writing(self) -> None:
        self._paused = False
        waiter = self._drain_waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(None)


class _DatagramBridgeProtocol(asyncio.DatagramProtocol):
    """Bridges ``asyncio.DatagramProtocol`` callbacks to
    ``DatagramReader`` / ``DatagramWriter``."""

    def __init__(self, reader: DatagramReader) -> None:
        self._reader = reader
        self._writer: DatagramWriter | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        pass  # transport is already wired via DatagramWriter

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._reader.feed_datagram(data, addr)

    def datagrams_received(self, data: list[bytes], addr: tuple[str, int]) -> None:
        self._reader.feed_datagrams(data, addr)

    def error_received(self, exc: Exception) -> None:
        self._reader.set_exception(exc)

    def connection_lost(self, exc: BaseException | None) -> None:
        self._reader.connection_lost(exc)
        if self._writer is not None:
            self._writer._closed_event.set()

    def pause_writing(self) -> None:
        if self._writer is not None:
            self._writer._pause_writing()

    def resume_writing(self) -> None:
        if self._writer is not None:
            self._writer._resume_writing()


async def open_dgram_connection(
    remote_addr: tuple[str, int] | None = None,
    *,
    local_addr: tuple[str, int] | None = None,
    family: int = socket.AF_UNSPEC,
    sock: socket.socket | None = None,
    gro_segment_size: int = 1280,
) -> tuple[DatagramReader, DatagramWriter]:
    loop = asyncio.get_running_loop()

    reader = DatagramReader()
    protocol = _DatagramBridgeProtocol(reader)

    transport, _ = await create_udp_endpoint(
        loop,
        lambda: protocol,
        local_addr=local_addr,
        remote_addr=remote_addr,
        family=family,
        gro_segment_size=gro_segment_size,
        sock=sock,
    )

    writer = DatagramWriter(transport)
    protocol._writer = writer

    return reader, writer
