# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import asyncio
from base64 import urlsafe_b64decode
from collections import Counter
import contextlib
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
        help="Exercise the Python UDP transport while retaining qh3 for HTTP/3",
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


class DNSUDPServer(typing.NamedTuple):
    address: tuple[str, int]
    requests: list[bytes]
    received: threading.Event
    respond: threading.Event


@pytest.fixture
def dns_udp_server(request: pytest.FixtureRequest) -> typing.Iterator[DNSUDPServer]:
    """Answer address/HTTPS queries over loopback with a parametrized TTL."""
    ttl = getattr(request, "param", 60)
    queries: list[bytes] = []
    errors: list[Exception] = []
    received, respond = threading.Event(), threading.Event()
    respond.set()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(10)
        address = sock.getsockname()

        def serve() -> None:
            try:
                while True:
                    query, client = sock.recvfrom(4096)
                    if not query:
                        return
                    queries.append(query)
                    received.set()
                    assert respond.wait(5), "DNS response was never released"
                    query_type, query_class = struct.unpack("!HH", query[-4:])
                    assert query_class == 1 and query_type in (1, 28, 65)
                    if query_type == 1:
                        data = socket.inet_pton(socket.AF_INET, "192.0.2.1")
                    elif query_type == 28:
                        data = socket.inet_pton(socket.AF_INET6, "2001:db8::1")
                    else:
                        data = b"\x00\x01\x00"  # HTTPS ServiceMode, original target.
                    # Echo the question; the answer name points back to its QNAME.
                    response = query[:2] + struct.pack("!HHHHH", 0x8180, 1, 1, 0, 0)
                    response += query[12:] + b"\xc0\x0c"
                    response += (
                        struct.pack("!HHIH", query_type, 1, ttl, len(data)) + data
                    )
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
