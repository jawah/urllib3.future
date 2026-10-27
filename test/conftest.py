# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import asyncio
from base64 import urlsafe_b64decode
from collections import Counter
import contextlib
import errno
import os
import socket
import ssl
import struct
import threading
import typing
from pathlib import Path

import pytest
import trustme
from tornado import httputil, web

from dummyserver.handlers import TestingApp
from dummyserver.proxy import ProxyHandler
from dummyserver.server import HAS_IPV6, run_loop_in_thread, run_tornado_app
from dummyserver.testcase import HTTPSDummyServerTestCase
from urllib3.backend._async.hface import _HAS_HTTP3_SUPPORT as _ASYNC_HAS_HTTP3_SUPPORT
from urllib3.backend.hface import _HAS_HTTP3_SUPPORT as _SYNC_HAS_HTTP3_SUPPORT
from urllib3.util import ssl_

from .tz_stub import stub_timezone_ctx


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--python-udp",
        action="store_true",
        help="Exercise the Python UDP transport while retaining qh3 for HTTP/3 and DoQ",
    )


@pytest.fixture
def python_udp_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        from qh3.asyncio import _transport
    except ImportError:
        return

    # Exercise the existing import fallback without replacing any socket I/O.
    monkeypatch.delattr(_transport, "OptimizedDatagramTransport", raising=False)


@pytest.fixture(autouse=True)
def select_udp_transport(request: pytest.FixtureRequest) -> None:
    if request.config.getoption("--python-udp"):
        request.getfixturevalue("python_udp_transport")


@pytest.fixture
def async_file_wrapper() -> typing.Callable[[typing.BinaryIO], typing.Any]:
    """Expose real file operations asynchronously without another I/O dependency."""

    class AsyncFile:
        def __init__(self, file: typing.BinaryIO) -> None:
            self.file = file

        async def read(self, size: int = -1) -> bytes:
            return await asyncio.get_running_loop().run_in_executor(
                None, self.file.read, size
            )

        async def tell(self) -> int:
            return await asyncio.get_running_loop().run_in_executor(
                None, self.file.tell
            )

        async def seek(self, offset: int) -> int:
            return await asyncio.get_running_loop().run_in_executor(
                None, self.file.seek, offset
            )

        def __aiter__(self) -> AsyncFile:
            return self

        async def __anext__(self) -> bytes:
            data = await self.read(65536)
            if not data:
                raise StopAsyncIteration
            return data

    return AsyncFile


@pytest.fixture(
    params=[
        "bytes",
        "text",
        "bytearray",
        "memoryview",
        "typed-memoryview",
        "strided-memoryview",
        "iterator",
    ]
)
def upload_body(request: pytest.FixtureRequest) -> tuple[typing.Any, bytes]:
    """Bodies larger than a protocol frame, with a distinct final tail."""
    payload = b"0123456789abcdef" * 8192 + b"tail"
    if request.param == "bytes":
        return payload, payload
    if request.param == "text":
        text = "Unicode: 🚀" + payload.decode()
        return text, text.encode()
    if request.param == "bytearray":
        return bytearray(payload), payload
    if request.param == "memoryview":
        return memoryview(payload), payload
    if request.param == "typed-memoryview":
        return memoryview(payload).cast("I"), payload
    if request.param == "strided-memoryview":
        return memoryview(payload)[::2], payload[::2]

    # The final input chunk must leave bytes to drain after iteration ends.
    chunks = [b"", "Unicode: 🚀", bytearray(b"prefix"), memoryview(payload), b""]
    return iter(chunks), "Unicode: 🚀prefix".encode() + payload


@pytest.fixture(params=["gzip", "deflate", "br", "zstd"])
def compressed_payload(request: pytest.FixtureRequest) -> tuple[str, bytes, bytes]:
    """Real compressed data, including consecutive nonempty Zstandard frames."""
    import gzip
    import zlib
    from urllib3 import response

    encoding = request.param
    first = bytes(range(256)) * 32
    second = b"another frame" * 1024
    plain = first + second
    if encoding == "gzip":
        encoded = gzip.compress(plain)
    elif encoding == "deflate":
        encoded = zlib.compress(plain)
    else:
        codec = response.brotli if encoding == "br" else response.zstd
        if codec is None:
            pytest.skip(f"{encoding} is unavailable")
        encoded = (
            codec.compress(plain)
            if encoding == "br"
            else codec.compress(first) + codec.compress(second)
        )
    return encoding, encoded, plain


class DNSUDPServer(typing.NamedTuple):
    address: tuple[str, int]
    requests: list[bytes]
    received: threading.Event
    respond: threading.Event


@pytest.fixture
def dns_udp_server(request: pytest.FixtureRequest) -> typing.Iterator[DNSUDPServer]:
    """Answer address/HTTPS queries over loopback with a parametrized TTL."""
    setting = getattr(request, "param", 60)
    mode = setting if isinstance(setting, str) else "normal"
    ttl = 0 if isinstance(setting, str) else setting
    queries: list[bytes] = []
    errors: list[Exception] = []
    received, respond = threading.Event(), threading.Event()
    respond.set()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        if hasattr(socket, "SIO_UDP_CONNRESET"):
            # Clients may close before our reply. A resulting ICMP error must
            # not terminate this shared UDP server on Windows.
            sock.ioctl(socket.SIO_UDP_CONNRESET, False)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(10)
        address = sock.getsockname()

        def serve() -> None:
            held_replies: list[tuple[bytes, typing.Any]] = []
            released_batch = False
            try:
                while True:
                    query, client = sock.recvfrom(4096)
                    if not query:
                        return
                    first_attempt = query not in queries
                    queries.append(query)
                    received.set()
                    if mode == "timeout" or (mode == "drop-first" and first_attempt):
                        continue
                    if mode != "reverse-two-lookups":
                        assert respond.wait(5), "DNS response was never released"
                    query_type, query_class = struct.unpack("!HH", query[-4:])
                    assert query_class == 1 and query_type in (1, 28, 65)
                    if query_type == 1:
                        data = socket.inet_pton(socket.AF_INET, "192.0.2.1")
                    elif query_type == 28:
                        data = socket.inet_pton(socket.AF_INET6, "2001:db8::1")
                    else:
                        data = b"\x00\x01\x00"  # HTTPS ServiceMode, original target.
                        if mode == "empty-https":
                            data = b""
                        elif mode == "https-ech":
                            data += b"\x00\x01\x00\x03\x02h3\x00\x05\x00\x04\x00\x02ab"
                    # Echo the question; the answer name points back to its QNAME.
                    response = query[:2] + struct.pack("!HHHHH", 0x8180, 1, 1, 0, 0)
                    response += query[12:] + b"\xc0\x0c"
                    response += (
                        struct.pack("!HHIH", query_type, 1, ttl, len(data)) + data
                    )
                    if mode == "garbage-first":
                        sock.sendto(b"garbage", client)
                    elif mode == "question-count":
                        sock.sendto(response[:4] + b"\x00\x00" + response[6:], client)
                    elif mode == "truncated-record":
                        sock.sendto(response[:-1], client)
                    elif mode == "truncated-answers":
                        sock.sendto(response[:6] + b"\x00\x02" + response[8:], client)
                    elif mode == "unknown-record":
                        question_end = len(query)
                        cname = (
                            b"\xc0\x0c" + struct.pack("!HHIH", 5, 1, 0, 2) + b"\xc0\x0c"
                        )
                        response = (
                            response[:6]
                            + b"\x00\x02"
                            + response[8:question_end]
                            + cname
                            + response[question_end:]
                        )
                    elif mode == "truncated-https" and query_type == 65:
                        response = response[:2] + b"\x83\x80" + response[4 : len(query)]
                    elif mode in ("servfail", "unknown-rcode", "no-answer"):
                        rcode = {"servfail": 2, "unknown-rcode": 15, "no-answer": 0}[
                            mode
                        ]
                        response = (
                            query[:2]
                            + struct.pack("!HHHHH", 0x8180 | rcode, 1, 0, 0, 0)
                            + query[12:]
                        )
                    if mode == "reverse-two-lookups" and not released_batch:
                        held_replies.append((response, client))
                        if len(held_replies) == 4:
                            assert respond.wait(5), "DNS batch was never released"
                            for packet, target in reversed(held_replies):
                                sock.sendto(packet, target)
                            released_batch = True
                        continue
                    sock.sendto(response, client)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield DNSUDPServer(address, queries, received, respond)
        finally:
            respond.set()
            # Wake recvfrom without depending on cross-thread socket.close semantics.
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as wakeup:
                wakeup.sendto(b"", address)
            thread.join(5)
            assert not thread.is_alive()
            assert not errors


class DNSQUICServer(typing.NamedTuple):
    address: tuple[str, int]
    ca_certs: str
    requests: list[bytes]
    received: threading.Event


@pytest.fixture
def doq_udp_socket(
    dns_quic_server: DNSQUICServer,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> typing.Iterator[list[socket.socket]]:
    """Keep real UDP I/O while rejecting the requested kernel offload operation."""
    from urllib3._constant import UDP_LINUX_GRO, UDP_LINUX_SEGMENT
    from urllib3.contrib.ssa._gro import _sock_has_gso

    mode = request.param
    if mode != "plain-udp":
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            if not _sock_has_gso(probe):
                pytest.skip("Kernel does not support UDP_SEGMENT")
    sockets: list[socket.socket] = []
    discarded = None
    if mode == "loss-then-reject-gso":
        discarded = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        discarded.bind(("127.0.0.1", 0))

    class Socket(socket.socket):
        gso_attempts = 0

        def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
            super().__init__(*args, **kwargs)
            if self.type == socket.SOCK_DGRAM:
                sockets.append(self)

        def setsockopt(self, level: int, option: int, *args: typing.Any) -> None:
            if (
                mode == "plain-udp"
                and level == socket.IPPROTO_UDP
                and option in (UDP_LINUX_GRO, UDP_LINUX_SEGMENT)
            ):
                raise OSError(errno.ENOPROTOOPT, "UDP offload unavailable")
            super().setsockopt(level, option, *args)

        def sendmsg(self, *args: typing.Any, **kwargs: typing.Any) -> int:
            self.gso_attempts += 1
            if discarded is not None and self.gso_attempts == 1:
                # Deliver the first flight to a silent UDP sink to exercise PTO.
                return super().sendmsg(args[0], args[1], 0, discarded.getsockname())
            # Emulate a NIC rejecting GSO after the kernel accepted its option.
            raise OSError(errno.EIO, "UDP segmentation rejected by the device")

    monkeypatch.setattr(socket, "socket", Socket)
    try:
        yield sockets
    finally:
        if discarded is not None:
            discarded.close()


@pytest.fixture
def doq_empty_ca_store(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from urllib3.contrib.anytls import ssl as tls_backend

    if tls_backend is not ssl or os.name == "nt":
        pytest.skip("Requires OpenSSL default CA paths controlled by environment")
    ca_file = tmp_path / "empty.pem"
    ca_file.write_text("")
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_file))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
    if request.param == "load-error":

        def load_default_certs(
            context: ssl.SSLContext, purpose: ssl.Purpose = ssl.Purpose.SERVER_AUTH
        ) -> None:
            # Produce a real OpenSSL loading error without replacing the context.
            context.load_verify_locations(cafile=str(ca_file))

        monkeypatch.setattr(ssl.SSLContext, "load_default_certs", load_default_certs)


@pytest.fixture
def default_ca_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> typing.Callable[[str, str], dict[str, typing.Any]]:
    """Configure real OpenSSL default trust without an eagerly loaded bundle."""
    import shutil
    import subprocess

    from urllib3.contrib.anytls import ssl as tls_backend

    openssl = shutil.which("openssl")
    if tls_backend is not ssl or os.name == "nt" or openssl is None:
        pytest.skip(
            "Requires OpenSSL CA paths controlled by environment and openssl CLI"
        )

    def configure(ca_data: str, mode: str) -> dict[str, typing.Any]:
        directory = tmp_path / "trust"
        directory.mkdir()
        ca_file = tmp_path / "authority.pem"
        ca_file.write_text(ca_data)
        subject_hash = subprocess.check_output(
            [openssl, "x509", "-in", str(ca_file), "-subject_hash", "-noout"],
            universal_newlines=True,
        ).strip()
        name = subject_hash + ".0" if mode != "unhashed" else "unconfigured.pem"
        (directory / name).write_text(ca_data)
        (directory / "README").write_text("Not a certificate")
        (directory / "11111111.0").write_text("Malformed certificate")
        (directory / "22222222.0").mkdir()
        (directory / "33333333.0").symlink_to(tmp_path / "missing.pem")
        wrong_ca = tmp_path / "other.pem"
        trustme.CA().cert_pem.write_to_path(wrong_ca)
        monkeypatch.setenv(
            "SSL_CERT_FILE",
            str(ca_file if mode == "bundle" else tmp_path / "missing.pem"),
        )
        capath = str(directory)
        if mode == "search-path":
            empty = tmp_path / "empty"
            empty.mkdir()
            capath = os.pathsep.join((str(empty), capath))
        monkeypatch.setenv("SSL_CERT_DIR", capath)
        assert len(ssl.create_default_context().get_ca_certs()) == (
            1 if mode == "bundle" else 0
        )

        if mode == "explicit-file":
            return {"ca_certs": str(wrong_ca)}
        if mode == "explicit-data":
            return {"ca_cert_data": wrong_ca.read_text()}
        if mode == "explicit-directory":
            wrong_dir = tmp_path / "wrong-trust"
            wrong_dir.mkdir()
            (wrong_dir / "0123abcd.0").write_bytes(wrong_ca.read_bytes())
            return {"ca_cert_dir": str(wrong_dir)}
        if mode == "explicit-context":
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.load_verify_locations(cafile=str(wrong_ca))
            return {"ssl_context": ctx}
        if mode == "unverified":
            return {"cert_reqs": ssl.CERT_NONE}
        return {}

    return configure


@pytest.fixture(params=[False, True], ids=["normal-close-timer", "delayed-close-timer"])
def doq_close_timer(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not request.param:
        return
    connection = pytest.importorskip("qh3.quic.connection").QuicConnection
    get_timer = connection.get_timer

    def delayed_close_timer(quic: typing.Any) -> float | None:
        deadline: float | None = get_timer(quic)
        if (
            deadline is not None
            and quic.configuration.is_client
            and getattr(quic, "_close_event", None) is not None
        ):
            # Keep the real TLS rejection pending beyond the socket timeout,
            # without slowing the handshake or fabricating protocol events.
            return deadline + 60
        return deadline

    monkeypatch.setattr(connection, "get_timer", delayed_close_timer)


@pytest.fixture
def dns_quic_server(
    request: pytest.FixtureRequest, tmp_path: Path
) -> typing.Iterator[DNSQUICServer]:
    """A real DoQ peer with explicit control over DNS framing and stream failures."""
    if ssl_.IS_FIPS:
        pytest.skip("DNS-over-QUIC is disabled with a FIPS-compliant ssl module")
    pytest.importorskip("urllib3.contrib.resolver.doq._qh3")
    from qh3.asyncio import QuicConnectionProtocol
    from qh3.asyncio.server import QuicServer
    from qh3.quic.configuration import QuicConfiguration
    from qh3.quic.events import StreamDataReceived

    root = Path(__file__).resolve().parents[1]
    ca_path = root / "rootCA.pem"
    cert_path = root / "traefik/httpbin.local.pem"
    key_path = root / "traefik/httpbin.local.pem.key"
    if not all(p.exists() for p in (ca_path, cert_path, key_path)):
        ca = trustme.CA()
        cert = ca.issue_cert("httpbin.local")
        ca_path, cert_path, key_path = (
            tmp_path / name for name in ("ca.pem", "server.pem", "server.key")
        )
        ca.cert_pem.write_to_path(ca_path)
        cert.cert_chain_pems[0].write_to_path(cert_path)
        cert.private_key_pem.write_to_path(key_path)

    mode = getattr(request, "param", "normal")
    queries: list[bytes] = []
    errors: list[BaseException] = []
    pending: set[asyncio.Task[None]] = set()
    received = threading.Event()

    class DNSProtocol(QuicConnectionProtocol):
        def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
            super().__init__(*args, **kwargs)
            self.buffers: dict[int, bytes] = {}
            self.delayed_replies: list[tuple[int, bytes]] = []

        def datagram_received(self, data: bytes | str, addr: typing.Any) -> None:
            self.client_address = addr
            super().datagram_received(data, addr)

        def quic_event_received(self, event: typing.Any) -> None:
            if not isinstance(event, StreamDataReceived):
                return
            stream_id = event.stream_id
            data = self.buffers.pop(stream_id, b"") + event.data
            if not event.end_stream:
                self.buffers[stream_id] = data
                return
            try:
                assert (
                    len(data) >= 2 and int.from_bytes(data[:2], "big") == len(data) - 2
                )
                query = data[2:]
                queries.append(query)
                if mode == "reset":
                    self._quic.reset_stream(stream_id, 0x2)
                elif mode == "close":
                    self._quic.close(error_code=0x2, reason_phrase="test peer closed")
                    self.transmit()
                    return
                else:
                    if mode == "stop-sending":
                        self._quic.stop_stream(stream_id, 0x2)
                    elif mode == "empty-datagram":
                        assert self._transport is not None
                        # asyncio transports may discard empty sends. Emit the
                        # actual zero-length UDP datagram from the bound socket.
                        udp_socket = self._transport.get_extra_info("socket")
                        getattr(udp_socket, "_sock", udp_socket).sendto(
                            b"", self.client_address
                        )
                    kind = int.from_bytes(query[-4:-2], "big")
                    if kind == 1:
                        rdata = socket.inet_pton(socket.AF_INET, "192.0.2.1")
                    elif kind == 28:
                        rdata = socket.inet_pton(socket.AF_INET6, "2001:db8::1")
                    else:
                        # HTTPS ServiceMode, with ALPN=h3 and a framed ECHConfigList.
                        rdata = b"\x00\x01\x00\x00\x01\x00\x03\x02h3\x00\x05\x00\x04\x00\x02ab"
                    rcode = 3 if mode == "nxdomain" else 0
                    count = 0 if mode in ("nxdomain", "no-answer") else 1
                    answer = (
                        query[:2]
                        + struct.pack("!HHHHH", 0x8180 | rcode, 1, count, 0, 0)
                        + query[12:]
                    )
                    if count:
                        answer += (
                            b"\xc0\x0c"
                            + struct.pack("!HHIH", kind, 1, 0, len(rdata))
                            + rdata
                        )
                    if mode == "malformed":
                        answer = answer[:5]
                    elif mode == "mismatch":
                        answer = b"\x00\x01" + answer[2:]
                    framed = len(answer).to_bytes(2, "big") + answer
                    if mode == "partial-prefix":
                        framed = framed[:1]
                    elif mode == "partial-body":
                        framed = framed[:-1]
                    elif mode == "multiple":
                        framed += framed
                    elif mode == "concurrent-malformed":
                        if query[12:16] == b"\x03bad":
                            framed = framed[:1]
                        if len(queries) <= 4:
                            self.delayed_replies.append((stream_id, framed))
                            # Both lookups have sent A + HTTPS before either gets
                            # a reply. Deliver the second lookup's errors first.
                            if len(self.delayed_replies) == 4:
                                for reply_id, reply in reversed(self.delayed_replies):
                                    self._quic.send_stream_data(
                                        reply_id, reply, end_stream=True
                                    )
                                self.delayed_replies.clear()
                                self.transmit()
                            return
                    if mode in ("split-prefix", "split-body"):
                        cut = 1 if mode == "split-prefix" else len(framed) // 2
                        self._quic.send_stream_data(
                            stream_id, framed[:cut], end_stream=False
                        )
                        self.transmit()

                        async def finish() -> None:
                            # An acknowledged PING proves the client received the first
                            # datagram before the rest can be sent. No timing sleep.
                            await self.ping()
                            self._quic.send_stream_data(
                                stream_id, framed[cut:], end_stream=True
                            )
                            self.transmit()

                        task = asyncio.create_task(finish())
                        pending.add(task)

                        def finished(task: asyncio.Task[None]) -> None:
                            pending.discard(task)
                            if not task.cancelled():
                                error = task.exception()
                                if error is not None:
                                    errors.append(error)

                        task.add_done_callback(finished)
                    else:
                        self._quic.send_stream_data(stream_id, framed, end_stream=True)
                self.transmit()
            except Exception as exc:
                errors.append(exc)
                self._quic.close(error_code=0x2, reason_phrase="test peer failure")
                self.transmit()

    class Server(QuicServer):
        def datagram_received(self, data: bytes | str, addr: typing.Any) -> None:
            received.set()
            if mode != "handshake-stall":
                super().datagram_received(data, addr)

    async def start() -> typing.Any:
        configuration = QuicConfiguration(is_client=False, alpn_protocols=["doq"])
        configuration.load_cert_chain(str(cert_path), str(key_path))
        _, server = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: Server(configuration=configuration, create_protocol=DNSProtocol),
            local_addr=("127.0.0.1", 0),
        )
        return server

    with run_loop_in_thread() as io_loop:
        loop = io_loop.asyncio_loop
        server = asyncio.run_coroutine_threadsafe(start(), loop).result(10)
        address = server._transport.get_extra_info("sockname")
        try:
            yield DNSQUICServer(address, str(ca_path), queries, received)
        finally:

            async def stop() -> None:
                tasks = list(pending)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
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
            assert not errors, errors


class DNSTLSServer(typing.NamedTuple):
    address: tuple[str, int]
    ca_certs: str
    requests: list[bytes]
    answer_count: int


@pytest.fixture
def dns_tls_server(
    request: pytest.FixtureRequest, tmp_path: Path
) -> typing.Iterator[DNSTLSServer]:
    """A TLS peer serving framed DNS replies or an explicit disconnect."""
    mode = request.param
    answer_count = 128 if mode == "large" else 1
    ca = trustme.CA()
    ca_path = str(tmp_path / "ca.pem")
    ca.cert_pem.write_to_path(ca_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ca.issue_cert("127.0.0.1").configure_cert(context)
    queries: list[bytes] = []
    errors: list[Exception] = []
    stop = threading.Event()

    def reply(query: bytes) -> bytes:
        query_type, query_class = struct.unpack("!HH", query[-4:])
        assert query_class == 1 and query_type in (1, 28, 65)
        records = []
        for i in range(1, answer_count + 1):
            if query_type == 1:
                records.append(socket.inet_pton(socket.AF_INET, f"192.0.2.{i}"))
            elif query_type == 28:
                records.append(socket.inet_pton(socket.AF_INET6, f"2001:db8::{i:x}"))
        body = query[:2] + struct.pack("!HHHHH", 0x8180, 1, len(records), 0, 0)
        body += query[12:]
        for data in records:
            body += (
                b"\xc0\x0c" + struct.pack("!HHIH", query_type, 1, 60, len(data)) + data
            )
        return struct.pack("!H", len(body)) + body

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)

        def serve() -> None:
            try:
                with listener.accept()[0] as raw:
                    raw.settimeout(5)
                    try:
                        conn = context.wrap_socket(raw, server_side=True)
                    except OSError as exc:
                        # A rejected certificate can surface as a TCP reset or
                        # errno 0 rather than an SSL error on some runtimes.
                        if mode == "handshake" and (
                            isinstance(exc, (ssl.SSLError, ConnectionResetError))
                            or exc.errno == 0
                        ):
                            return
                        raise
                    with conn, conn.makefile("rb") as reader:
                        replies: list[bytes] = []
                        while True:
                            try:
                                prefix = reader.read(2)
                            except ConnectionResetError:
                                # Resolver.close() may reset the idle TLS connection.
                                if replies:
                                    raise
                                return
                            if not prefix:
                                return
                            assert len(prefix) == 2
                            size = struct.unpack("!H", prefix)[0]
                            query = reader.read(size)
                            assert len(query) == size
                            queries.append(query)
                            replies.append(reply(query))
                            # Gather the lookup's A, AAAA and HTTPS questions.
                            if len(replies) < 3:
                                continue
                            if mode in ("empty", "partial-prefix", "partial-body"):
                                partial = {
                                    "empty": b"",
                                    "partial-prefix": b"\x00",
                                    "partial-body": b"\x00\x64partial",
                                }[mode]
                                if partial:
                                    conn.sendall(partial)
                                # Send close_notify, then close TCP without waiting
                                # for the client's reciprocal shutdown alert.
                                conn.setblocking(False)
                                with contextlib.suppress(
                                    ssl.SSLWantReadError,
                                    BlockingIOError,  # Older LibreSSL reports would-block directly.
                                    ssl.SSLEOFError,
                                    ssl.SSLSyscallError,
                                    ConnectionResetError,
                                ):
                                    try:
                                        conn.unwrap().close()
                                    except OSError as exc:
                                        # OpenSSL 1.0.2 can report peer close as errno 0.
                                        if exc.errno != 0:
                                            raise
                                return
                            if mode == "timeout":
                                assert stop.wait(10), "Timed-out client never closed"
                                return
                            # Reverse the replies to exercise transaction-ID matching.
                            payload = b"".join(reversed(replies))
                            if mode == "malformed":
                                payload = b"\x00\x03bad" + payload
                            conn.sendall(payload)
                            replies.clear()
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield DNSTLSServer(listener.getsockname(), ca_path, queries, answer_count)
        finally:
            stop.set()
            thread.join(10)
            assert not thread.is_alive(), "DNS TLS peer did not finish"
            assert not errors, errors


class ServerConfig(typing.NamedTuple):
    scheme: str
    host: str
    port: int
    ca_certs: str | None
    intermediate: bytes | None

    @property
    def base_url(self) -> str:
        host = self.host
        if ":" in host:
            host = f"[{host}]"
        return f"{self.scheme}://{host}:{self.port}"


@pytest.fixture
def http3_error_server(
    tmp_path: Path, error_code: int, frame_type: int | None, reason: str
) -> typing.Iterator[ServerConfig]:
    """Send a QUIC close frame after receiving an actual HTTP/3 request."""
    if not (_SYNC_HAS_HTTP3_SUPPORT() and _ASYNC_HAS_HTTP3_SUPPORT()):
        pytest.skip("HTTP/3 unavailable")
    from qh3 import H3Connection, QuicConfiguration, h3_events
    from qh3.asyncio import QuicConnectionProtocol
    from qh3.asyncio.server import QuicServer

    ca = trustme.CA()
    cert = ca.issue_cert("127.0.0.1")
    ca_path = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(ca_path)
    certs = _write_cert_to_dir(cert, tmp_path)
    errors: list[Exception] = []
    received = threading.Event()
    pending: list[asyncio.Task[None]] = []

    class Protocol(QuicConnectionProtocol):
        def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
            super().__init__(*args, **kwargs)
            self.http = H3Connection(self._quic)

        def quic_event_received(self, event: typing.Any) -> None:
            try:
                for http_event in self.http.handle_event(event):
                    if isinstance(http_event, h3_events.HeadersReceived):
                        received.set()
                        pending.append(asyncio.create_task(self.close_after_setup()))
            except Exception as exc:
                errors.append(exc)
                self.close()

        async def close_after_setup(self) -> None:
            # Confirm delivery of the server's SETTINGS before closing, so the
            # client does not have to finish HTTP/3 setup on an already closed QUIC.
            await self.ping()
            self._quic.close(
                error_code=error_code, frame_type=frame_type, reason_phrase=reason
            )
            self.transmit()

    async def start() -> typing.Any:
        configuration = QuicConfiguration(
            is_client=False, alpn_protocols=["h3"], max_datagram_frame_size=65536
        )
        configuration.load_cert_chain(certs["certfile"], certs["keyfile"])
        _, server = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: QuicServer(configuration=configuration, create_protocol=Protocol),
            local_addr=("127.0.0.1", 0),
        )
        return server

    with run_loop_in_thread() as io_loop:
        loop = io_loop.asyncio_loop
        server = asyncio.run_coroutine_threadsafe(start(), loop).result(10)
        host, port = server._transport.get_extra_info("sockname")
        try:
            yield ServerConfig("https", host, port, str(ca_path), None)
        finally:

            async def stop() -> None:
                for task in pending:
                    if not task.done():
                        task.cancel()
                results = await asyncio.gather(*pending, return_exceptions=True)
                errors.extend(
                    result for result in results if isinstance(result, Exception)
                )
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
            assert not errors, errors
            assert received.is_set(), "The HTTP/3 peer did not receive the request"


def _write_cert_to_dir(
    cert: trustme.LeafCert, tmpdir: Path, file_prefix: str = "server"
) -> dict[str, str]:
    cert_path = str(tmpdir / ("%s.pem" % file_prefix))
    key_path = str(tmpdir / ("%s.key" % file_prefix))
    cert.private_key_pem.write_to_path(key_path)
    cert.cert_chain_pems[0].write_to_path(cert_path)
    certs = {"keyfile": key_path, "certfile": cert_path}
    return certs


class DNSHTTPSServer(typing.NamedTuple):
    config: ServerConfig
    proxy_url: str
    requests: list[httputil.HTTPServerRequest]
    proxy_requests: list[httputil.HTTPServerRequest]
    https_records: list[str | bytes]


@pytest.fixture
def dns_https_server(
    request: pytest.FixtureRequest, tmp_path: Path
) -> typing.Iterator[DNSHTTPSServer]:
    """Local DoH endpoint, optionally serving a parametrized HTTP status/body."""
    response = getattr(request, "param", None)
    requests: list[httputil.HTTPServerRequest] = []
    proxy_requests: list[httputil.HTTPServerRequest] = []
    https_records: list[str | bytes] = []
    ca = trustme.CA()
    ca_path = str(tmp_path / "ca.pem")
    ca.cert_pem.write_to_path(ca_path)
    certs = _write_cert_to_dir(ca.issue_cert("127.0.0.1"), tmp_path)

    class DoHHandler(web.RequestHandler):
        def get(self) -> None:
            requests.append(self.request)
            if response is not None:
                status, body = response
                self.set_status(status)
                self.write(body)
                return
            dns = self.get_query_argument("dns", None)
            if dns is not None:
                query = urlsafe_b64decode(dns + "=" * (-len(dns) % 4))
                query_type = struct.unpack("!H", query[-4:-2])[0]
                self.set_header("Content-Type", "application/dns-message")
                if query_type == 65:
                    records = https_records
                else:
                    family = socket.AF_INET if query_type == 1 else socket.AF_INET6
                    address = "192.0.2.1" if query_type == 1 else "2001:db8::1"
                    records = [socket.inet_pton(family, address)]
                body = (
                    query[:2]
                    + struct.pack("!HHHHH", 0x8180, 1, len(records), 0, 0)
                    + query[12:]
                )
                for data in records:
                    assert isinstance(data, bytes)
                    body += (
                        b"\xc0\x0c"
                        + struct.pack("!HHIH", query_type, 1, 60, len(data))
                        + data
                    )
                self.write(body)
            else:
                name = self.get_query_argument("name")
                query_type = int(self.get_query_argument("type"))
                self.set_header("Content-Type", "application/dns-json")
                records = (
                    https_records
                    if query_type == 65
                    else ["192.0.2.1" if query_type == 1 else "2001:db8::1"]
                )
                self.write(
                    {
                        "Status": 0,
                        "Question": [{"name": name, "type": query_type}],
                        "Answer": [
                            {
                                "name": name,
                                "type": query_type,
                                "TTL": 60,
                                "data": data,
                            }
                            for data in records
                        ],
                    }
                )

    class RecordingProxy(ProxyHandler):
        async def connect(self) -> None:
            proxy_requests.append(self.request)
            await super().connect()

    with run_loop_in_thread() as io_loop:

        async def run_app() -> tuple[int, int]:
            _, port = run_tornado_app(
                web.Application([(r".*", DoHHandler)]), certs, "https", "127.0.0.1"
            )
            _, proxy_port = run_tornado_app(
                web.Application([(r".*", RecordingProxy)]), None, "http", "127.0.0.1"
            )
            return port, proxy_port

        port, proxy_port = asyncio.run_coroutine_threadsafe(
            run_app(), io_loop.asyncio_loop
        ).result(5)
        yield DNSHTTPSServer(
            ServerConfig("https", "127.0.0.1", port, ca_path, None),
            f"http://127.0.0.1:{proxy_port}",
            requests,
            proxy_requests,
            https_records,
        )


@contextlib.contextmanager
def run_server_in_thread(
    scheme: str,
    host: str,
    tmpdir: Path | None,
    ca: trustme.CA | None,
    server_cert: trustme.LeafCert | None,
    intermediate: trustme.CA | None = None,
) -> typing.Generator[ServerConfig, None, None]:
    if ca is not None and server_cert is not None and tmpdir is not None:
        ca_cert_path = str(tmpdir / "ca.pem")
        ca.cert_pem.write_to_path(ca_cert_path)
        server_certs = _write_cert_to_dir(server_cert, tmpdir)
    else:
        assert scheme == "http"
        ca_cert_path = None
        server_certs = {}

    with run_loop_in_thread() as io_loop:

        async def run_app() -> int:
            app = web.Application([(r".*", TestingApp)])
            server, port = run_tornado_app(app, server_certs, scheme, host)
            return port

        port = asyncio.run_coroutine_threadsafe(
            run_app(),
            io_loop.asyncio_loop,
        ).result()
        yield ServerConfig(
            scheme,
            host,
            port,
            ca_cert_path,
            None if intermediate is None else intermediate.cert_pem.bytes(),
        )


@contextlib.contextmanager
def run_server_and_proxy_in_thread(
    proxy_scheme: str,
    proxy_host: str,
    tmpdir: Path,
    ca: trustme.CA,
    proxy_cert: trustme.LeafCert,
    server_cert: trustme.LeafCert,
) -> typing.Generator[tuple[ServerConfig, ServerConfig], None, None]:
    ca_cert_path = str(tmpdir / "ca.pem")
    ca.cert_pem.write_to_path(ca_cert_path)

    server_certs = _write_cert_to_dir(server_cert, tmpdir)
    proxy_certs = _write_cert_to_dir(proxy_cert, tmpdir, "proxy")

    with run_loop_in_thread() as io_loop:

        async def run_app() -> tuple[ServerConfig, ServerConfig]:
            app = web.Application([(r".*", TestingApp)])
            server_app, port = run_tornado_app(app, server_certs, "https", "localhost")
            server_config = ServerConfig("https", "localhost", port, ca_cert_path, None)

            proxy = web.Application([(r".*", ProxyHandler)])
            proxy_app, proxy_port = run_tornado_app(
                proxy, proxy_certs, proxy_scheme, proxy_host
            )
            proxy_config = ServerConfig(
                proxy_scheme, proxy_host, proxy_port, ca_cert_path, None
            )
            return proxy_config, server_config

        proxy_config, server_config = asyncio.run_coroutine_threadsafe(
            run_app(),
            io_loop.asyncio_loop,
        ).result()
        yield (proxy_config, server_config)


@pytest.fixture(params=["localhost", "127.0.0.1", "::1"])
def loopback_host(request: typing.Any) -> typing.Generator[str, None, None]:
    host = request.param
    if host == "::1" and not HAS_IPV6:
        pytest.skip("Test requires IPv6 on loopback")
    yield host


@pytest.fixture()
def san_server(
    loopback_host: str, tmp_path_factory: pytest.TempPathFactory
) -> typing.Generator[ServerConfig, None, None]:
    tmpdir = tmp_path_factory.mktemp("certs")
    ca = trustme.CA()

    server_cert = ca.issue_cert(loopback_host)

    with run_server_in_thread("https", loopback_host, tmpdir, ca, server_cert) as cfg:
        yield cfg


@pytest.fixture()
def broken_intermediate_server(
    loopback_host: str, tmp_path_factory: pytest.TempPathFactory
) -> typing.Generator[ServerConfig, None, None]:
    tmpdir = tmp_path_factory.mktemp("certs")
    ca = trustme.CA()

    intermediate = ca.create_child_ca()

    server_cert = intermediate.issue_cert(loopback_host)

    with run_server_in_thread(
        "https", loopback_host, tmpdir, ca, server_cert, intermediate
    ) as cfg:
        yield cfg


@pytest.fixture()
def no_san_server(
    loopback_host: str, tmp_path_factory: pytest.TempPathFactory
) -> typing.Generator[ServerConfig, None, None]:
    tmpdir = tmp_path_factory.mktemp("certs")
    ca = trustme.CA()
    server_cert = ca.issue_cert(common_name=loopback_host)

    with run_server_in_thread("https", loopback_host, tmpdir, ca, server_cert) as cfg:
        yield cfg


@pytest.fixture()
def no_san_server_with_different_commmon_name(
    tmp_path_factory: pytest.TempPathFactory,
) -> typing.Generator[ServerConfig, None, None]:
    tmpdir = tmp_path_factory.mktemp("certs")
    ca = trustme.CA()
    server_cert = ca.issue_cert(common_name="example.com")

    with run_server_in_thread("https", "localhost", tmpdir, ca, server_cert) as cfg:
        yield cfg


@pytest.fixture
def san_proxy_with_server(
    loopback_host: str, tmp_path_factory: pytest.TempPathFactory
) -> typing.Generator[tuple[ServerConfig, ServerConfig], None, None]:
    tmpdir = tmp_path_factory.mktemp("certs")
    ca = trustme.CA()
    proxy_cert = ca.issue_cert(loopback_host)
    server_cert = ca.issue_cert("localhost")

    with run_server_and_proxy_in_thread(
        "https", loopback_host, tmpdir, ca, proxy_cert, server_cert
    ) as cfg:
        yield cfg


@pytest.fixture
def no_san_proxy_with_server(
    tmp_path_factory: pytest.TempPathFactory,
) -> typing.Generator[tuple[ServerConfig, ServerConfig], None, None]:
    tmpdir = tmp_path_factory.mktemp("certs")
    ca = trustme.CA()
    # only common name, no subject alternative names
    proxy_cert = ca.issue_cert(common_name="localhost")
    server_cert = ca.issue_cert("localhost")

    with run_server_and_proxy_in_thread(
        "https", "localhost", tmpdir, ca, proxy_cert, server_cert
    ) as cfg:
        yield cfg


@pytest.fixture
def no_localhost_san_server(
    tmp_path_factory: pytest.TempPathFactory,
) -> typing.Generator[ServerConfig, None, None]:
    tmpdir = tmp_path_factory.mktemp("certs")
    ca = trustme.CA()
    # non localhost common name
    server_cert = ca.issue_cert("example.com")

    with run_server_in_thread("https", "localhost", tmpdir, ca, server_cert) as cfg:
        yield cfg


@pytest.fixture
def ipv4_san_proxy_with_server(
    tmp_path_factory: pytest.TempPathFactory,
) -> typing.Generator[tuple[ServerConfig, ServerConfig], None, None]:
    tmpdir = tmp_path_factory.mktemp("certs")
    ca = trustme.CA()
    # IP address in Subject Alternative Name
    proxy_cert = ca.issue_cert("127.0.0.1")

    server_cert = ca.issue_cert("localhost")

    with run_server_and_proxy_in_thread(
        "https", "127.0.0.1", tmpdir, ca, proxy_cert, server_cert
    ) as cfg:
        yield cfg


@pytest.fixture
def ipv6_san_proxy_with_server(
    tmp_path_factory: pytest.TempPathFactory,
) -> typing.Generator[tuple[ServerConfig, ServerConfig], None, None]:
    tmpdir = tmp_path_factory.mktemp("certs")
    ca = trustme.CA()
    # IP addresses in Subject Alternative Name
    proxy_cert = ca.issue_cert("::1")

    server_cert = ca.issue_cert("localhost")

    with run_server_and_proxy_in_thread(
        "https", "::1", tmpdir, ca, proxy_cert, server_cert
    ) as cfg:
        yield cfg


@pytest.fixture
def ipv4_san_server(
    tmp_path_factory: pytest.TempPathFactory,
) -> typing.Generator[ServerConfig, None, None]:
    tmpdir = tmp_path_factory.mktemp("certs")
    ca = trustme.CA()
    # IP address in Subject Alternative Name
    server_cert = ca.issue_cert("127.0.0.1")

    with run_server_in_thread("https", "127.0.0.1", tmpdir, ca, server_cert) as cfg:
        yield cfg


@pytest.fixture
def ipv6_plain_server() -> typing.Generator[ServerConfig, None, None]:
    if not HAS_IPV6:
        pytest.skip("Only runs on IPv6 systems")

    with run_server_in_thread("http", "::1", None, None, None) as cfg:
        yield cfg


@pytest.fixture
def ipv6_san_server(
    tmp_path_factory: pytest.TempPathFactory,
) -> typing.Generator[ServerConfig, None, None]:
    if not HAS_IPV6:
        pytest.skip("Only runs on IPv6 systems")

    tmpdir = tmp_path_factory.mktemp("certs")
    ca = trustme.CA()
    # IP address in Subject Alternative Name
    server_cert = ca.issue_cert("::1")

    with run_server_in_thread("https", "::1", tmpdir, ca, server_cert) as cfg:
        yield cfg


@pytest.fixture
def ipv6_no_san_server(
    tmp_path_factory: pytest.TempPathFactory,
) -> typing.Generator[ServerConfig, None, None]:
    if not HAS_IPV6:
        pytest.skip("Only runs on IPv6 systems")

    tmpdir = tmp_path_factory.mktemp("certs")
    ca = trustme.CA()
    # IP address in Common Name
    server_cert = ca.issue_cert(common_name="::1")

    with run_server_in_thread("https", "::1", tmpdir, ca, server_cert) as cfg:
        yield cfg


@pytest.fixture
def stub_timezone(request: pytest.FixtureRequest) -> typing.Generator[None, None, None]:
    """
    A pytest fixture that runs the test with a stub timezone.
    """
    with stub_timezone_ctx(request.param):
        yield


@pytest.fixture(scope="session")
def supported_tls_versions() -> typing.AbstractSet[str | None]:
    # We have to create an actual TLS connection
    # to test if the TLS version is not disabled by
    # OpenSSL config. Ubuntu 20.04 specifically
    # disables TLSv1 and TLSv1.1.
    tls_versions = set()

    _server = HTTPSDummyServerTestCase()
    _server._start_server()
    for _ssl_version_name, min_max_version in (
        ("PROTOCOL_TLSv1", ssl.TLSVersion.TLSv1),
        ("PROTOCOL_TLSv1_1", ssl.TLSVersion.TLSv1_1),
        ("PROTOCOL_TLSv1_2", ssl.TLSVersion.TLSv1_2),
        ("PROTOCOL_TLS", None),
    ):
        _ssl_version = getattr(ssl, _ssl_version_name, 0)
        if _ssl_version == 0:
            continue
        _sock = socket.create_connection((_server.host, _server.port))
        try:
            _sock = ssl_.ssl_wrap_socket(
                _sock,
                ssl_context=ssl_.create_urllib3_context(
                    cert_reqs=ssl.CERT_NONE,
                    ssl_minimum_version=min_max_version,
                    ssl_maximum_version=min_max_version,
                ),
            )
        except ssl.SSLError:
            pass
        else:
            tls_versions.add(_sock.version())
        _sock.close()
    _server._stop_server()
    return tls_versions


@pytest.fixture(scope="function")
def requires_tlsv1(supported_tls_versions: typing.AbstractSet[str]) -> None:
    """Test requires TLSv1 available"""
    if not hasattr(ssl, "PROTOCOL_TLSv1") or "TLSv1" not in supported_tls_versions:
        pytest.skip("Test requires TLSv1")


@pytest.fixture(scope="function")
def requires_tlsv1_1(supported_tls_versions: typing.AbstractSet[str]) -> None:
    """Test requires TLSv1.1 available"""
    if not hasattr(ssl, "PROTOCOL_TLSv1_1") or "TLSv1.1" not in supported_tls_versions:
        pytest.skip("Test requires TLSv1.1")


@pytest.fixture(scope="function")
def requires_tlsv1_2(supported_tls_versions: typing.AbstractSet[str]) -> None:
    """Test requires TLSv1.2 available"""
    if not hasattr(ssl, "PROTOCOL_TLSv1_2") or "TLSv1.2" not in supported_tls_versions:
        pytest.skip("Test requires TLSv1.2")


@pytest.fixture(scope="function")
def requires_tlsv1_3(supported_tls_versions: typing.AbstractSet[str]) -> None:
    """Test requires TLSv1.3 available"""
    if (
        not getattr(ssl, "HAS_TLSv1_3", False)
        or "TLSv1.3" not in supported_tls_versions
    ):
        pytest.skip("Test requires TLSv1.3")


_TRAEFIK_AVAILABLE = None


@pytest.fixture(scope="session")
def requires_traefik() -> None:
    global _TRAEFIK_AVAILABLE

    if _TRAEFIK_AVAILABLE is not None:
        if _TRAEFIK_AVAILABLE is False:
            pytest.skip(
                "Test requires Traefik server (HTTP/2 over TCP and HTTP/3 over QUIC)"
            )
        return

    try:
        sock = socket.create_connection(
            (os.environ.get("TRAEFIK_HTTPBIN_IPV4", "127.0.0.1"), 8888), timeout=1
        )
    except (ConnectionRefusedError, socket.gaierror, TimeoutError):
        _TRAEFIK_AVAILABLE = False
        pytest.skip(
            "Test requires Traefik server (HTTP/2 over TCP and HTTP/3 over QUIC)"
        )
    else:
        _TRAEFIK_AVAILABLE = True
        sock.shutdown(0)
        sock.close()


@pytest.fixture(scope="function")
def requires_http3(for_async: bool = False) -> None:
    _TARGET_METHOD = (
        _SYNC_HAS_HTTP3_SUPPORT if not for_async else _ASYNC_HAS_HTTP3_SUPPORT
    )

    if _TARGET_METHOD() is False:
        pytest.skip("Test requires HTTP/3 support")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_terminal_summary(
    terminalreporter: typing.Any,
    exitstatus: int,
    config: pytest.Config,
) -> typing.Generator[None, None, None]:
    """Fold skipped tests by reason instead of source location."""
    reportchars = terminalreporter.reportchars
    terminalreporter.reportchars = reportchars.replace("s", "")

    try:
        yield
    finally:
        terminalreporter.reportchars = reportchars
        skipped = terminalreporter.stats.get("skipped", [])
        reasons: Counter[str] = Counter()

        for report in skipped:
            longrepr = report.longrepr
            if isinstance(longrepr, tuple) and len(longrepr) == 3:
                reason = str(longrepr[2])
            else:
                reason = str(longrepr)

            if reason.startswith("Skipped: "):
                reason = reason[len("Skipped: ") :]
            reasons[reason] += 1

        if reasons:
            terminalreporter.write_sep(
                "=", "skipped tests by reason", yellow=True, bold=True
            )
            for reason, count in reasons.most_common():
                terminalreporter.write_line(f"SKIPPED [{count}] {reason}", yellow=True)


if os.environ.get("XDIST_DEBUG"):
    from datetime import datetime
    import signal
    import threading

    # Global dictionary to track worker states
    WORKER_STATES: dict[str, dict[str, str]] = {}
    WORKER_LOCK = threading.Lock()

    def pytest_configure(config):  # type: ignore[no-untyped-def]
        """Register signal handler for CTRL+C to dump worker states."""
        if hasattr(config, "workerinput"):
            # We're in a worker
            worker_id = config.workerinput.get("workerid", "unknown")

            def signal_handler(signum, frame):  # type: ignore[no-untyped-def]
                print(
                    f"\n[{worker_id}] Interrupted! Last known test: {WORKER_STATES.get(worker_id, 'unknown')}"
                )

            signal.signal(signal.SIGINT, signal_handler)

    def pytest_runtest_logstart(nodeid, location):  # type: ignore[no-untyped-def]
        """Called when a test starts running."""
        worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")

        with WORKER_LOCK:
            WORKER_STATES[worker_id] = {
                "test": nodeid,
                "start_time": datetime.now().isoformat(),
                "location": location,
            }

        # Also log to a file for persistent tracking
        log_file = Path(f".pytest_worker_{worker_id}.log")
        with open(log_file, "a") as f:
            f.write(f"{datetime.now().isoformat()} START: {nodeid}\n")
            f.flush()

    def pytest_runtest_logfinish(nodeid, location):  # type: ignore[no-untyped-def]
        """Called when a test finishes."""
        worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")

        # Log completion
        log_file = Path(f".pytest_worker_{worker_id}.log")
        with open(log_file, "a") as f:
            f.write(f"{datetime.now().isoformat()} FINISH: {nodeid}\n")
            f.flush()

    def pytest_sessionfinish(session):  # type: ignore[no-untyped-def]
        """Clean up log files after session."""
        worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
        log_file = Path(f".pytest_worker_{worker_id}.log")
        if log_file.exists():
            log_file.unlink()
