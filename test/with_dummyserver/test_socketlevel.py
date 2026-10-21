# TODO: Break this module up into pieces. Maybe group by functionality tested
# rather than the socket level-ness of it.
from __future__ import annotations

import contextlib
import errno
import io
import os
import os.path
import platform
import select
import shutil
import socket
import ssl
import sys
import tempfile
import threading
import time
import typing
import warnings
import zlib
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from test import (
    LONG_TIMEOUT,
    SHORT_TIMEOUT,
    notMacOS,
    notWindows,
    resolvesLocalhostFQDN,
)
from threading import Event
from unittest import mock

import pytest
from jh2.config import H2Configuration  # type: ignore[import-untyped]
from jh2.connection import H2Connection  # type: ignore[import-untyped]
from jh2.events import RequestReceived  # type: ignore[import-untyped]
from urllib3 import HttpVersion, ResponsePromise
import trustme

from dummyserver.server import (
    DEFAULT_CA,
    DEFAULT_CERTS,
    encrypt_key_pem,
    get_unreachable_address,
)
from dummyserver.testcase import SocketDummyServerTestCase, consume_socket
from test.conftest import ServerConfig
from urllib3 import (
    HTTPConnectionPool,
    HTTPResponse,
    HTTPSConnectionPool,
    PoolManager,
    ProxyManager,
    util,
)
from urllib3._collections import HTTPHeaderDict
from urllib3.connection import HTTPConnection, _get_default_user_agent
from urllib3.connectionpool import _url_from_pool
from urllib3.contrib.webextensions.raw import RawExtensionFromHTTP
from urllib3.contrib.webextensions.sse import (
    ServerSentEvent,
    ServerSideEventExtensionFromHTTP,
)
from urllib3.exceptions import (
    IncompleteRead,
    InsecureRequestWarning,
    InvalidHeader,
    MaxRetryError,
    ProtocolError,
    ProxyError,
    ReadTimeoutError,
    SSLError,
)
from urllib3.poolmanager import proxy_from_url
from urllib3.util import ssl_, ssl_wrap_socket
from urllib3.util.retry import Retry
from urllib3.util.timeout import Timeout
from urllib3.util.wait import wait_for_read

from .. import LogRecorder, has_alpn

if typing.TYPE_CHECKING:
    from _typeshed import StrOrBytesPath
else:
    StrOrBytesPath = object


class TestConsumeSocket:
    @pytest.mark.parametrize("prefix", [b"", b"GET / HTTP/1.1\r\nHost: localhost\r\n"])
    def test_eof_before_headers(self, prefix: bytes) -> None:
        reader, writer = socket.socketpair()
        with reader, writer, ThreadPoolExecutor(1) as executor:
            reader.settimeout(5)
            writer.sendall(prefix)
            writer.shutdown(socket.SHUT_WR)
            reading = executor.submit(consume_socket, reader)
            try:
                with pytest.raises(ConnectionAbortedError, match="HTTP headers"):
                    reading.result(5)
            finally:
                # Also unwind the old implementation if it spins on EOF.
                reader.close()

    @pytest.mark.parametrize("chunks", [1, 65536])
    def test_header_delimiter_across_reads_and_before_body(self, chunks: int) -> None:
        headers = b"POST / HTTP/1.1\r\nContent-Length: 4\r\n\r\n"
        payload = headers + b"body"
        reader, writer = socket.socketpair()
        with reader, writer, ThreadPoolExecutor(1) as executor:
            reader.settimeout(5)
            writer.sendall(payload)
            writer.shutdown(socket.SHUT_WR)
            reading = executor.submit(consume_socket, reader, chunks)
            try:
                consumed = reading.result(5)
                assert consumed.startswith(headers)
                if chunks == 1:
                    assert consumed == headers
                with reader.makefile("rb") as remaining:
                    assert consumed + remaining.read() == payload
            finally:
                reader.close()


class TestCookies(SocketDummyServerTestCase):
    def test_multi_setcookie(self) -> None:
        def multicookie_response_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += sock.recv(65536)

            sock.send(
                b"HTTP/1.1 200 OK\r\nSet-Cookie: foo=1\r\nSet-Cookie: bar=1\r\n\r\n"
            )
            sock.close()

        self._start_server(multicookie_response_handler)
        with HTTPConnectionPool(self.host, self.port) as pool:
            r = pool.request("GET", "/", retries=0)
            assert r.headers == {"set-cookie": "foo=1, bar=1"}
            assert r.headers.getlist("set-cookie") == ["foo=1", "bar=1"]


class TestSNI(SocketDummyServerTestCase):
    def test_hostname_in_first_request_packet(self) -> None:
        done_receiving = Event()
        self.buf = b""

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            self.buf = sock.recv(65536)  # We only accept one packet
            done_receiving.set()  # let the test know it can proceed
            sock.close()

        self._start_server(socket_handler)
        with HTTPSConnectionPool(self.host, self.port) as pool:
            try:
                pool.request("GET", "/", retries=0)
            except MaxRetryError:  # We are violating the protocol
                pass
            successful = done_receiving.wait(LONG_TIMEOUT)
            assert successful, "Timed out waiting for connection accept"
            assert self.host.encode("ascii") in self.buf, (
                "missing hostname in SSL handshake"
            )


class TestALPN(SocketDummyServerTestCase):
    def test_alpn_protocol_in_first_request_packet(self) -> None:
        if not has_alpn():
            pytest.skip("ALPN-support not available")

        done_receiving = Event()
        self.buf = b""

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            self.buf = sock.recv(65536)  # We only accept one packet
            done_receiving.set()  # let the test know it can proceed
            sock.close()

        self._start_server(socket_handler)
        with HTTPSConnectionPool(self.host, self.port) as pool:
            try:
                pool.request("GET", "/", retries=0)
            except MaxRetryError:  # We are violating the protocol
                pass
            successful = done_receiving.wait(LONG_TIMEOUT)
            assert successful, "Timed out waiting for connection accept"
            for protocol in util.ALPN_PROTOCOLS:
                assert protocol.encode("ascii") in self.buf, (
                    "missing ALPN protocol in SSL handshake"
                )


def original_ssl_wrap_socket(
    sock: socket.socket,
    keyfile: StrOrBytesPath | None = None,
    certfile: StrOrBytesPath | None = None,
    server_side: bool = False,
    cert_reqs: ssl.VerifyMode = ssl.CERT_NONE,
    ssl_version: int | str = "PROTOCOL_TLS",
    ca_certs: str | None = None,
    do_handshake_on_connect: bool = True,
    suppress_ragged_eofs: bool = True,
    ciphers: str | None = None,
) -> ssl.SSLSocket:
    _major, _minor, _patch = ssl.OPENSSL_VERSION_INFO[:3]
    is_broken_old_ssl: bool = (
        "OpenSSL" in ssl.OPENSSL_VERSION and _major < 1 or (_major == 1 and _minor < 1)
    )
    ssl_version = ssl_.resolve_ssl_version(  # type: ignore[call-overload]
        ssl_version, mitigate_tls_version=not is_broken_old_ssl
    )
    if server_side and not certfile:
        raise ValueError("certfile must be specified for server-side operations")
    if keyfile and not certfile:
        raise ValueError("certfile must be specified")
    if (
        hasattr(ssl.SSLContext, "minimum_version")
        and hasattr(ssl, "PROTOCOL_TLS_SERVER")
        and is_broken_old_ssl is False
    ):
        context = ssl.SSLContext(
            ssl.PROTOCOL_TLS_SERVER if server_side else ssl.PROTOCOL_TLS_CLIENT
        )
        if hasattr(ssl, "TLSVersion") and isinstance(ssl_version, ssl.TLSVersion):
            context.minimum_version = ssl_version
            context.maximum_version = ssl_version
    else:
        context = ssl.SSLContext(ssl_version)  # type: ignore[arg-type]

    context.verify_mode = cert_reqs
    if ca_certs:
        context.load_verify_locations(ca_certs)
    if certfile:
        context.load_cert_chain(certfile, keyfile)
    if ciphers:
        context.set_ciphers(ciphers)
    return context.wrap_socket(
        sock=sock,
        server_side=server_side,
        do_handshake_on_connect=do_handshake_on_connect,
        suppress_ragged_eofs=suppress_ragged_eofs,
    )


class TestClientCerts(SocketDummyServerTestCase):
    @pytest.mark.parametrize("invalid", ["weak-certificate", "unsupported-key"])
    def test_native_client_identity_assignment_rejected(self, invalid: str) -> None:
        from datetime import datetime, timedelta, timezone
        from io import UnsupportedOperation

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa, x25519
        from urllib3.contrib.anytls import BACKEND
        from urllib3.contrib.imcc._ctypes import _OpenSSL

        # LibreSSL's compatibility version does not imply security-level support.
        if (
            BACKEND != "ssl"
            or not ssl.OPENSSL_VERSION.startswith("OpenSSL ")
            or ssl_.IS_FIPS
            or ssl.OPENSSL_VERSION_INFO < (1, 1, 1)
        ):
            pytest.skip("Requires stdlib OpenSSL with configurable security levels")
        try:
            _OpenSSL()
        except UnsupportedOperation:
            pytest.skip("The native ctypes certificate loader is unavailable")

        key: rsa.RSAPrivateKey | x25519.X25519PrivateKey
        if invalid == "weak-certificate":
            key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
            subject = x509.Name(
                [x509.NameAttribute(x509.NameOID.COMMON_NAME, "localhost")]
            )
            now = datetime.now(timezone.utc)
            certificate = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1))
                .not_valid_after(now + timedelta(days=1))
                .sign(key, hashes.SHA256())
            )
            cert_data = certificate.public_bytes(serialization.Encoding.PEM)
            expected_error = "ee key too small"
        else:
            # X25519 is a valid private-key encoding, but not a TLS signing key.
            key = x25519.X25519PrivateKey.generate()
            cert_data = Path(self.cert_path).read_bytes()
            expected_error = "unknown certificate type"
        key_data = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        context = ssl.create_default_context(cafile=self.ca_path)
        context.set_ciphers("DEFAULT:@SECLEVEL=2")
        peer_closed = Event()

        def handler(listener: socket.socket) -> None:
            listener.settimeout(10)
            with listener.accept()[0] as sock:
                sock.settimeout(10)
                assert sock.recv(1) == b""
                peer_closed.set()

        self._start_server(handler)
        with HTTPSConnectionPool(
            self.host,
            self.port,
            ssl_context=context,
            cert_data=cert_data,
            key_data=key_data,
            timeout=5,
            retries=False,
        ) as pool:
            with pytest.raises(SSLError, match=expected_error):
                pool.request("GET", "/")
        assert peer_closed.wait(10)

    @pytest.mark.parametrize("invalid", ["certificate", "key", "mismatch"])
    def test_native_client_identity_failure_closes_socket(self, invalid: str) -> None:
        from io import UnsupportedOperation
        from urllib3.contrib.anytls import BACKEND
        from urllib3.contrib.imcc._ctypes import _OpenSSL

        if BACKEND != "ssl":
            pytest.skip("This regression targets the stdlib OpenSSL certificate loader")
        try:
            _OpenSSL()
        except UnsupportedOperation:
            pytest.skip("The native ctypes certificate loader is unavailable")
        cert = Path(self.cert_path).read_bytes()
        key = Path(self.key_path).read_bytes()
        if invalid == "certificate":
            cert = b"-----BEGIN CERTIFICATE-----\ninvalid\n-----END CERTIFICATE-----\n"
        elif invalid == "key":
            key = b"-----BEGIN PRIVATE KEY-----\ninvalid\n-----END PRIVATE KEY-----\n"
        else:
            key = Path(DEFAULT_CERTS["keyfile"]).read_bytes()
        peer_closed = Event()

        def handler(listener: socket.socket) -> None:
            with listener.accept()[0] as sock:
                sock.settimeout(5)
                assert sock.recv(1) == b""
                peer_closed.set()

        self._start_server(handler)
        with HTTPSConnectionPool(
            self.host,
            self.port,
            ca_certs=self.ca_path,
            cert_data=cert,
            key_data=key,
            timeout=5,
            retries=False,
        ) as pool:
            with pytest.raises((SSLError, ProtocolError)) as caught:
                pool.request("GET", "/")
            if isinstance(caught.value, ProtocolError):
                # The FIPS build wraps the native SSL error as a connection abort.
                assert len(caught.value.args) == 2
                assert isinstance(caught.value.args[1], ssl.SSLError)
        assert peer_closed.wait(5)

    """
    Tests for client certificate support.
    """

    @classmethod
    def setup_class(cls) -> None:
        cls.tmpdir = tempfile.mkdtemp()
        ca = trustme.CA()
        cert = ca.issue_cert("localhost")
        encrypted_key = encrypt_key_pem(cert.private_key_pem, b"letmein")

        cls.ca_path = os.path.join(cls.tmpdir, "ca.pem")
        cls.cert_combined_path = os.path.join(cls.tmpdir, "server.combined.pem")
        cls.cert_path = os.path.join(cls.tmpdir, "server.pem")
        cls.key_path = os.path.join(cls.tmpdir, "key.pem")
        cls.password_key_path = os.path.join(cls.tmpdir, "password_key.pem")

        ca.cert_pem.write_to_path(cls.ca_path)
        cert.private_key_and_cert_chain_pem.write_to_path(cls.cert_combined_path)
        cert.cert_chain_pems[0].write_to_path(cls.cert_path)
        cert.private_key_pem.write_to_path(cls.key_path)
        encrypted_key.write_to_path(cls.password_key_path)

    @classmethod
    def teardown_class(cls) -> None:
        shutil.rmtree(cls.tmpdir)

    def _wrap_in_ssl(self, sock: socket.socket) -> ssl.SSLSocket:
        """
        Given a single socket, wraps it in TLS.
        """
        return original_ssl_wrap_socket(
            sock,
            ssl_version=ssl.PROTOCOL_SSLv23,
            cert_reqs=ssl.CERT_REQUIRED,
            ca_certs=self.ca_path,
            certfile=self.cert_path,
            keyfile=self.key_path,
            server_side=True,
        )

    def test_client_certs_two_files(self) -> None:
        """
        Having a client cert in a separate file to its associated key works
        properly.
        """
        done_receiving = Event()
        client_certs = []

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            sock = self._wrap_in_ssl(sock)

            client_certs.append(sock.getpeercert())

            data = b""
            while not data.endswith(b"\r\n\r\n"):
                data += sock.recv(8192)

            sock.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Server: testsocket\r\n"
                b"Connection: close\r\n"
                b"Content-Length: 6\r\n"
                b"\r\n"
                b"Valid!"
            )

            done_receiving.wait(5)
            sock.close()

        self._start_server(socket_handler)
        with HTTPSConnectionPool(
            self.host,
            self.port,
            cert_file=self.cert_path,
            key_file=self.key_path,
            cert_reqs="REQUIRED",
            ca_certs=self.ca_path,
        ) as pool:
            pool.request("GET", "/", retries=0)
            done_receiving.set()

            assert len(client_certs) == 1

    def test_client_certs_one_file(self) -> None:
        """
        Having a client cert and its associated private key in just one file
        works properly.
        """
        done_receiving = Event()
        client_certs = []

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            sock = self._wrap_in_ssl(sock)

            client_certs.append(sock.getpeercert())

            data = b""
            while not data.endswith(b"\r\n\r\n"):
                data += sock.recv(8192)

            sock.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Server: testsocket\r\n"
                b"Connection: close\r\n"
                b"Content-Length: 6\r\n"
                b"\r\n"
                b"Valid!"
            )

            done_receiving.wait(5)
            sock.close()

        self._start_server(socket_handler)
        with HTTPSConnectionPool(
            self.host,
            self.port,
            cert_file=self.cert_combined_path,
            cert_reqs="REQUIRED",
            ca_certs=self.ca_path,
        ) as pool:
            pool.request("GET", "/", retries=0)
            done_receiving.set()

            assert len(client_certs) == 1

    @pytest.mark.skipif(platform.system() != "Linux", reason="Requires Linux /dev/shm")
    @pytest.mark.skipif(
        sys.implementation.name == "pypy",
        reason="PyPy libffi does not implement _shm_open (probable bug)",
    )
    def test_client_cert_shared_memory_without_proc(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from urllib3.contrib.imcc import _shm

        if not os.path.isdir("/dev/shm") or not os.access("/dev/shm", os.W_OK):
            pytest.skip("Requires writable /dev/shm")
        # Converting a stdlib context to utls/rtls cannot copy its client identity.
        ctx = ssl_.create_urllib3_context()
        ctx.load_verify_locations(self.ca_path)
        exists = os.path.exists
        shared_paths: set[str] = set()

        def without_proc(path: str) -> bool:
            if path.startswith("/proc/self/fd/"):
                return False
            if path.startswith("/dev/shm/"):
                shared_paths.add(path)
            return exists(path)

        with monkeypatch.context() as patch:
            patch.delattr(os, "memfd_create", raising=False)
            patch.setattr(os.path, "exists", without_proc)
            _shm.load_cert_chain(
                ctx, Path(self.cert_path).read_bytes(), Path(self.key_path).read_bytes()
            )
        assert len(shared_paths) == 1
        assert all(not exists(path) for path in shared_paths)

        client_certs = []

        def handler(listener: socket.socket) -> None:
            listener.settimeout(5)
            with listener.accept()[0] as sock:
                sock.settimeout(5)
                with self._wrap_in_ssl(sock) as tls:
                    client_certs.append(tls.getpeercert(binary_form=True))
                    headers = bytearray()
                    while not headers.endswith(b"\r\n\r\n"):
                        chunk = tls.recv(65536)
                        assert chunk
                        headers.extend(chunk)
                    tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")

        self._start_server(handler)
        with HTTPSConnectionPool(
            self.host, self.port, ssl_context=ctx, timeout=5, retries=False
        ) as pool:
            assert pool.request("GET", "/").data == b"ok"
        expected = ssl.PEM_cert_to_DER_cert(Path(self.cert_path).read_text())
        assert client_certs == [expected]

    def test_missing_client_certs_raises_error(self) -> None:
        """
        Having client certs not be present causes an error.
        """
        done_receiving = Event()

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            try:
                self._wrap_in_ssl(sock)
            except ssl.SSLError:
                pass

            done_receiving.wait(5)
            sock.close()

        self._start_server(socket_handler)
        with HTTPSConnectionPool(
            self.host, self.port, cert_reqs="REQUIRED", ca_certs=self.ca_path
        ) as pool:
            with pytest.raises(MaxRetryError):
                pool.request("GET", "/", retries=0)
                done_receiving.set()
            done_receiving.set()

    def test_client_cert_with_string_password(self) -> None:
        self.run_client_cert_with_password_test("letmein")

    def test_client_cert_with_bytes_password(self) -> None:
        self.run_client_cert_with_password_test(b"letmein")

    def run_client_cert_with_password_test(self, password: bytes | str) -> None:
        """
        Tests client certificate password functionality
        """
        done_receiving = Event()
        client_certs = []

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            sock = self._wrap_in_ssl(sock)

            client_certs.append(sock.getpeercert())

            data = b""
            while not data.endswith(b"\r\n\r\n"):
                data += sock.recv(8192)

            sock.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Server: testsocket\r\n"
                b"Connection: close\r\n"
                b"Content-Length: 6\r\n"
                b"\r\n"
                b"Valid!"
            )

            done_receiving.wait(5)
            sock.close()

        self._start_server(socket_handler)
        assert ssl_.SSLContext is not None
        ssl_context = ssl_.SSLContext(ssl_.PROTOCOL_TLS_CLIENT)
        ssl_context.load_cert_chain(
            certfile=self.cert_path, keyfile=self.password_key_path, password=password
        )

        with HTTPSConnectionPool(
            self.host,
            self.port,
            ssl_context=ssl_context,
            cert_reqs="REQUIRED",
            ca_certs=self.ca_path,
        ) as pool:
            pool.request("GET", "/", retries=0)
            done_receiving.set()

            assert len(client_certs) == 1

    @pytest.mark.xfail(
        platform.python_version().startswith("3.7."),
        raises=BlockingIOError,
        reason="CPython 3.7.x bug",
        strict=False,
    )
    def test_load_keyfile_with_invalid_password(self) -> None:
        assert ssl_.SSLContext is not None
        context = ssl_.SSLContext(ssl_.PROTOCOL_TLS_CLIENT)
        with pytest.raises(ssl.SSLError):
            context.load_cert_chain(
                certfile=self.cert_path,
                keyfile=self.password_key_path,
                password=b"letmei",
            )


class TestRawExtension(SocketDummyServerTestCase):
    @pytest.mark.parametrize("message", ["hello 🚀", b"hello\x00world"])
    def test_timeout_then_echo(self, message: str | bytes) -> None:
        expected = message.encode() if isinstance(message, str) else message

        def socket_handler(listener: socket.socket) -> None:
            with listener.accept()[0] as sock:
                sock.settimeout(5)
                consume_socket(sock)
                sock.sendall(
                    b"HTTP/1.1 101 Switching Protocols\r\n"
                    b"Connection: Upgrade\r\nUpgrade: echo\r\n\r\n"
                )
                data = b""
                while len(data) < len(expected):
                    chunk = sock.recv(65536)
                    assert chunk
                    data += chunk
                assert data == expected
                sock.sendall(data)
                assert sock.recv(1) == b""

        self._start_server(socket_handler)
        with PoolManager(timeout=5) as manager:
            response = manager.urlopen(
                "GET",
                f"http://{self.host}:{self.port}/",
                headers={"Connection": "Upgrade", "Upgrade": "echo"},
                extension=RawExtensionFromHTTP(),
            )
            assert response.status == 101
            extension = response.extension
            assert isinstance(extension, RawExtensionFromHTTP)
            assert response._police_officer is not None
            with response._police_officer.borrow(response) as conn:
                assert conn.sock is not None
                conn.sock.settimeout(0.05)
            # The peer sends nothing until we write, so this timeout is deterministic.
            with pytest.raises(ReadTimeoutError):
                extension.next_payload()
            assert not extension.closed
            with response._police_officer.borrow(response) as conn:
                assert conn.sock is not None
                conn.sock.settimeout(5)
            extension.send_payload(message)
            received = b""
            while len(received) < len(expected):
                chunk = extension.next_payload()
                assert chunk
                received += chunk
            assert received == expected
            extension.close()
            extension.close()
            assert extension.closed
            with pytest.raises(OSError, match="closed"):
                extension.next_payload()
            with pytest.raises(OSError, match="closed"):
                extension.send_payload(message)


class TestServerSentEvents(SocketDummyServerTestCase):
    def test_extension_request_headers(self) -> None:
        extension_request_headers = {
            "Accept": "text/event-stream; charset=utf-8",
            "X-Client": "preserved",
        }
        received = []

        def handler(listener: socket.socket) -> None:
            with listener.accept()[0] as sock:
                sock.settimeout(5)
                headers = bytearray()
                while not headers.endswith(b"\r\n\r\n"):
                    data = sock.recv(65536)
                    assert data
                    headers.extend(data)
                received.append(bytes(headers))
                sock.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Content-Length: 10\r\n\r\ndata: ok\n\n"
                )

        self._start_server(handler)
        with HTTPConnectionPool(self.host, self.port, timeout=5) as pool:
            response = pool.urlopen(
                "GET",
                "/",
                headers=extension_request_headers,
                extension=ServerSideEventExtensionFromHTTP(),
                preload_content=False,
            )
            assert response.extension is not None
            event = response.extension.next_payload()
            assert isinstance(event, ServerSentEvent) and event.data == "ok"
            response.extension.close()
        assert b"Accept: text/event-stream; charset=utf-8\r\n" in received[0]
        assert b"X-Client: preserved\r\n" in received[0]
        assert dict(extension_request_headers) == {
            "Accept": "text/event-stream; charset=utf-8",
            "X-Client": "preserved",
        }

    @pytest.mark.parametrize("blocksize", [1, 65536])
    @pytest.mark.parametrize("raw", [False, True])
    def test_event_fields_and_boundaries(self, blocksize: int, raw: bool) -> None:
        events = [
            'event: update\r\nid: cursor-1\r\nretry: 1500\r\ndata: {"text": "🚀"}\r\n\r\n',
            "unknown: ignored\nid: bad\x00id\nretry: soon\ndata: second\n\n",
            "data:third\n\n",
        ]
        body = (": heartbeat\r\n\r\n" + "".join(events)).encode()
        self.start_response_handler(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )

        # One-byte reads also split UTF-8 code points and event separators.
        with PoolManager(blocksize=blocksize, timeout=5) as manager:
            response = manager.request("GET", f"psse://{self.host}:{self.port}/")
            extension = response.extension
            assert isinstance(extension, ServerSideEventExtensionFromHTTP)
            if raw:
                assert [extension.next_payload(raw=True) for _ in events] == events
            else:
                first, second, third = [extension.next_payload() for _ in events]
                assert isinstance(first, ServerSentEvent)
                assert isinstance(second, ServerSentEvent)
                assert isinstance(third, ServerSentEvent)
                assert first.event == "update"
                assert first.json() == {"text": "🚀"}
                assert first.retry == 1500
                assert first.id == second.id == third.id == "cursor-1"
                assert second.event == third.event == "message"
                assert second.data == "second"
                assert third.data == "third"
                assert second.retry is None
                assert "retry=1500" in repr(first)
            assert extension.next_payload() is None
            assert extension.closed
            extension.close()
            extension.close()
            with pytest.raises(OSError, match="closed"):
                extension.next_payload()


class TestSocketClosing(SocketDummyServerTestCase):
    @pytest.mark.filterwarnings("ignore:unclosed <socket.socket:ResourceWarning")
    @pytest.mark.parametrize("release_conn", [False, True])
    def test_unread_response_finalization_does_not_block_server(
        self, release_conn: bool
    ) -> None:
        import gc
        from weakref import ref

        from urllib3.util.traffic_police import TrafficPolice

        abandoned: list[HTTPResponse] = []
        finish_collection, peer_closed = Event(), Event()
        errors: list[BaseException] = []

        def handler(listener: socket.socket) -> None:
            try:
                listener.settimeout(10)
                with listener.accept()[0] as first:
                    first.settimeout(10)
                    consume_socket(first)
                    first.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\none")
                    with listener.accept()[0] as second:
                        second.settimeout(10)
                        consume_socket(second)
                        # Collection on a server thread must not wait for a
                        # checkout held by the client waiting for this response.
                        abandoned.clear()
                        gc.collect()
                        second.sendall(
                            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
                        )
                        assert finish_collection.wait(10)
                        # Intentionally abandoned sockets warn during GC;
                        # peer EOF below verifies that their descriptors close.
                        gc.collect()
                        gc.collect()
                        assert first.recv(1) == b""
                        peer_closed.set()
            except BaseException as exc:
                errors.append(exc)

        self._start_server(handler)

        def abandon_response() -> tuple[
            TrafficPolice[HTTPConnection], tuple[typing.Callable[[], object], ...]
        ]:
            with HTTPConnectionPool(
                self.host,
                self.port,
                timeout=5,
                retries=False,
                background_watch_delay=None,
            ) as pool:
                response = pool.request(
                    "GET", "/", preload_content=False, release_conn=release_conn
                )
                police = pool.pool
                conn = response.connection
                assert police is not None and conn is not None
                assert not response.closed
                # Delay CPython's destruction too; PyPy already uses tracing GC.
                response._gc_cycle = response  # type: ignore[attr-defined]
                abandoned.append(response)
                references = (ref(response), ref(pool), ref(police), ref(conn))
            return police, references

        # Retain only the registry as a way to unwind a regressed finalizer.
        police: TrafficPolice[HTTPConnection] | None
        police, references = abandon_response()
        try:
            with HTTPConnectionPool(
                self.host,
                self.port,
                timeout=5,
                retries=False,
                background_watch_delay=None,
            ) as other:
                assert other.request("GET", "/").data == b"ok"
            police = None
            finish_collection.set()
            assert peer_closed.wait(10), errors
            assert all(reference() is None for reference in references)
        finally:
            if police is not None:
                police.release()
            finish_collection.set()
            self.server_thread.join(10)
        assert not self.server_thread.is_alive()
        assert not errors, errors

    @pytest.mark.parametrize("overlap_close", [False, True])
    def test_close_before_response_headers_releases_socket(
        self, monkeypatch: pytest.MonkeyPatch, overlap_close: bool
    ) -> None:
        from urllib3.exceptions import HTTPError

        request_received, peer_closed, send_eof = Event(), Event(), Event()

        def handler(listener: socket.socket) -> None:
            listener.settimeout(5)
            with listener.accept()[0] as sock:
                sock.settimeout(5)
                headers = bytearray()
                while not headers.endswith(b"\r\n\r\n"):
                    chunk = sock.recv(65536)
                    assert chunk
                    headers.extend(chunk)
                request_received.set()
                if overlap_close:
                    assert send_eof.wait(5)
                    sock.shutdown(socket.SHUT_WR)
                assert sock.recv(1) == b""
                peer_closed.set()

        self._start_server(handler)
        with PoolManager(timeout=5, retries=False) as manager, ThreadPoolExecutor(
            1
        ) as executor:
            request = executor.submit(
                manager.request, "GET", f"http://{self.host}:{self.port}/"
            )
            assert request_received.wait(5)
            manager_cleared, cleanup_started = Event(), Event()
            if overlap_close:
                pool = next(iter(manager.pools._registry.values()))
                assert pool.pool is not None
                conn = next(iter(pool.pool._registry.values()))
                client_sock = conn.sock
                closing_thread = threading.get_ident()
                shutdown = socket.socket.shutdown

                def synchronized_shutdown(sock: socket.socket, how: int) -> None:
                    if sock is client_sock:
                        if threading.get_ident() == closing_thread:
                            # Peer EOF wakes recv() on every OS; local SHUT_RD
                            # need not interrupt another thread's recv() on Windows.
                            send_eof.set()
                            assert cleanup_started.wait(5)
                        else:
                            cleanup_started.set()
                            assert manager_cleared.wait(5)
                    shutdown(sock, how)

                monkeypatch.setattr(socket.socket, "shutdown", synchronized_shutdown)
            try:
                manager.clear()
            finally:
                send_eof.set()
                manager_cleared.set()
            with pytest.raises(HTTPError) as caught:
                request.result(5)
            if overlap_close:
                # The forced overlap must not hide a cleanup failure. An arbitrary
                # pre-headers close may invalidate other request state instead.
                assert not isinstance(caught.value.__cause__, AttributeError)
            assert peer_closed.wait(5)
            assert manager.pools.rsize() == 0

    def test_manager_clear_reaches_busy_child_pool(self) -> None:
        finish_body, first_closed, second_closed = Event(), Event(), Event()

        def read_request(sock: socket.socket) -> None:
            sock.settimeout(5)
            headers = bytearray()
            while not headers.endswith(b"\r\n\r\n"):
                chunk = sock.recv(65536)
                assert chunk
                headers.extend(chunk)

        def handler(listener: socket.socket) -> None:
            listener.settimeout(5)
            with listener.accept()[0] as first:
                read_request(first)
                first.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\naa")
                with listener.accept()[0] as second:
                    read_request(second)
                    second.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nb")
                    try:
                        assert first.recv(1) == b""
                    except ConnectionResetError:
                        pass
                    first_closed.set()
                    assert finish_body.wait(5)
                    try:
                        second.sendall(b"b")
                        assert second.recv(1) == b""
                    except (ConnectionResetError, BrokenPipeError):
                        pass
                    second_closed.set()

        self._start_server(handler)
        connections = []
        try:
            with PoolManager(timeout=5, retries=False) as manager:
                url = f"http://{self.host}:{self.port}/"
                first = manager.request("GET", url, preload_content=False)
                assert first.connection is not None
                connections.append(first.connection)
                second = manager.request("GET", url, preload_content=False)
                assert second.connection is not None
                connections.append(second.connection)
                assert connections[0] is not connections[1]
                first_sock, second_sock = [conn.sock for conn in connections]
                assert first_sock is not None and second_sock is not None
                police = second._police_officer
                assert police is not None
                assert first.read() == b"aa"
                assert first_sock.fileno() >= 0
                manager.clear()
                assert police._shutdown
                assert first_sock.fileno() == -1
                assert first_closed.wait(5)
                assert second_sock.fileno() >= 0
                finish_body.set()
                assert second.read() == b"bb"
                assert second_sock.fileno() == -1
                assert second_closed.wait(5)
                assert not police._shutdown
        finally:
            finish_body.set()
            for conn in connections:
                conn.close()

    def test_manager_clear_during_request_cleans_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reading_body, finish_body, peer_closed = Event(), Event(), Event()
        raw_read = HTTPResponse._raw_read

        def read_body(
            response: HTTPResponse, *args: typing.Any, **kwargs: typing.Any
        ) -> bytes:
            # Signal once the response exists and the client starts reading its body.
            reading_body.set()
            return raw_read(response, *args, **kwargs)

        monkeypatch.setattr(HTTPResponse, "_raw_read", read_body)

        def handler(listener: socket.socket) -> None:
            listener.settimeout(5)
            with listener.accept()[0] as sock:
                sock.settimeout(5)
                headers = bytearray()
                while not headers.endswith(b"\r\n\r\n"):
                    chunk = sock.recv(65536)
                    assert chunk
                    headers.extend(chunk)
                sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\no")
                assert finish_body.wait(5)
                try:
                    sock.sendall(b"k")
                    assert sock.recv(1) == b""
                except (BrokenPipeError, ConnectionResetError):
                    pass
                peer_closed.set()

        self._start_server(handler)
        with ThreadPoolExecutor(1) as executor, PoolManager(
            timeout=5, retries=False
        ) as manager:
            future = executor.submit(
                manager.request, "GET", f"http://{self.host}:{self.port}/"
            )
            try:
                assert reading_body.wait(5)
                police = next(iter(manager.pools._registry.values())).pool
                assert police is not None
                manager.clear()
            finally:
                finish_body.set()
            response = future.result(5)
            assert response.data == b"ok"
            assert peer_closed.wait(5)
            assert police.rsize() == 0
            assert manager.pools.rsize() == 0

    @pytest.mark.parametrize(
        "owner_kind", ["pool", "manager", "direct-child", "adapter"]
    )
    @pytest.mark.parametrize("close_owner", [False, True])
    @pytest.mark.parametrize("stream", [False, True])
    def test_streamed_response_outlives_owner(
        self, owner_kind: str, close_owner: bool, stream: bool
    ) -> None:
        finish_body, peer_closed = Event(), Event()
        body = b"first" + b"x" * 1024

        def handler(listener: socket.socket) -> None:
            listener.settimeout(5)
            with listener.accept()[0] as sock:
                sock.settimeout(5)
                for index in range(1 if close_owner else 2):
                    headers = bytearray()
                    while not headers.endswith(b"\r\n\r\n"):
                        chunk = sock.recv(65536)
                        if not chunk:
                            return
                        headers.extend(chunk)
                    sock.sendall(
                        f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode()
                        + body[:5]
                    )
                    if index == 0:
                        assert finish_body.wait(5)
                    sock.sendall(body[5:])
                assert sock.recv(1) == b""
                peer_closed.set()

        self._start_server(handler)
        owner = (
            PoolManager(timeout=5, retries=False)
            if owner_kind != "pool"
            else HTTPConnectionPool(self.host, self.port, timeout=5, retries=False)
        )
        url = f"http://{self.host}:{self.port}/" if owner_kind != "pool" else "/"
        conn = None
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(owner)
                requester = owner
                if owner_kind in ("direct-child", "adapter"):
                    assert isinstance(owner, PoolManager)
                    # Requests gets a child pool directly instead of calling manager.urlopen().
                    requester = owner.connection_from_url(url)
                    url = "/"
                response = requester.request("GET", url, preload_content=False)
                if owner_kind == "adapter":
                    assert isinstance(owner, PoolManager)
                    assert isinstance(requester, HTTPConnectionPool)
                    # Niquests returns the parent checkout before reading the body.
                    owner.pools.memorize(response, requester)
                    owner.pools.release()
                    assert not owner.pools.busy
                conn = response.connection
                assert conn is not None and conn.sock is not None
                http_pool = response._pool
                assert http_pool is not None
                watcher = http_pool._background_monitoring
                assert watcher is not None
                # Keep strong references: GC must not close this socket.
                sock = conn.sock
                if close_owner:
                    stack.close()
                    assert http_pool.pool is None
                    watcher.join(1)
                    assert not watcher.is_alive()
                assert sock.fileno() >= 0
                assert response.read(1) == body[:1]
                assert sock.fileno() >= 0
                finish_body.set()
                remaining = b"".join(response.stream(17)) if stream else response.read()
                assert remaining == body[1:]
                if isinstance(owner, PoolManager):
                    assert id(response) not in owner.pools._map
                if close_owner:
                    assert sock.fileno() == -1
                    if isinstance(owner, PoolManager):
                        assert owner.pools.rsize() == 0
                else:
                    assert conn.sock is sock and sock.fileno() >= 0
                response.close()
                if not close_owner:
                    if owner_kind == "adapter":
                        assert isinstance(owner, PoolManager)
                        requester = owner.connection_from_url(
                            f"http://{self.host}:{self.port}/"
                        )
                    followup = requester.request("GET", url, preload_content=False)
                    if owner_kind == "adapter":
                        assert isinstance(owner, PoolManager)
                        assert isinstance(requester, HTTPConnectionPool)
                        owner.pools.memorize(followup, requester)
                        owner.pools.release()
                    assert followup.data == body
                    assert conn.sock is sock
            assert peer_closed.wait(5), "Client did not close the retired connection"
        finally:
            finish_body.set()
            if conn is not None:
                conn.close()

    def test_idle_tls_close_notify_before_reuse(self) -> None:
        close_first = Event()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # Keep post-handshake TLS 1.3 tickets out of the readiness check.
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(DEFAULT_CERTS["certfile"], DEFAULT_CERTS["keyfile"])

        def handler(listener: socket.socket) -> None:
            listener.settimeout(5)
            for i in range(2):
                with listener.accept()[0] as raw:
                    raw.settimeout(5)
                    with context.wrap_socket(raw, server_side=True) as sock:
                        request = bytearray()
                        while not request.endswith(b"\r\n\r\n"):
                            data = sock.recv(65536)
                            assert data
                            request.extend(data)
                        sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                        if i == 0:
                            assert close_first.wait(5)
                        # Send close_notify without closing TCP; wait for the client.
                        try:
                            sock.unwrap().close()
                        except (ssl.SSLError, ConnectionResetError, BlockingIOError):
                            # Older LibreSSL can report EAGAIN after the peer closes.
                            pass
                        except OSError as exc:
                            # OpenSSL 1.0.2 can report the client's TCP close as errno 0.
                            if exc.errno != 0:
                                raise

        self._start_server(handler)
        with HTTPSConnectionPool(
            self.host,
            self.port,
            # Exercise stdlib TLS EOF, including when an alternative is installed.
            ssl_backend="ssl",
            ca_certs=DEFAULT_CA,
            timeout=5,
            background_watch_delay=None,
            retries=False,
        ) as pool:
            assert pool.request("GET", "/").status == 200
            close_first.set()
            assert pool.pool is not None
            with pool.pool.borrow() as conn:
                previous = conn.sock
                assert previous is not None
                assert wait_for_read(previous, timeout=5)
            response = pool.request("GET", "/")
            assert response.data == b"ok"
            with pool.pool.borrow() as conn:
                assert conn.sock is not None and conn.sock is not previous

    @pytest.mark.parametrize("peer_event", ["goaway", "invalid-ping"])
    def test_idle_http2_event_before_reuse(self, peer_event: str) -> None:
        send_event = Event()
        event_sent = Event()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(DEFAULT_CERTS["certfile"], DEFAULT_CERTS["keyfile"])
        context.set_alpn_protocols(["h2"])

        def handler(listener: socket.socket) -> None:
            listener.settimeout(5)
            for index in range(2):
                with listener.accept()[0] as raw:
                    raw.settimeout(5)
                    with context.wrap_socket(raw, server_side=True) as sock:
                        h2 = H2Connection(config=H2Configuration(client_side=False))
                        h2.initiate_connection()
                        sock.sendall(h2.data_to_send())
                        responded = False
                        while not responded:
                            data = sock.recv(65536)
                            assert data
                            for event in h2.receive_data(data):
                                if isinstance(event, RequestReceived):
                                    h2.send_headers(
                                        event.stream_id,
                                        [
                                            (b":status", b"200"),
                                            (b"content-length", b"2"),
                                        ],
                                    )
                                    h2.send_data(
                                        event.stream_id, b"ok", end_stream=True
                                    )
                                    responded = True
                            sock.sendall(h2.data_to_send())
                        if index == 0:
                            assert send_event.wait(5)
                            if peer_event == "goaway":
                                h2.close_connection(last_stream_id=1)
                                sock.sendall(h2.data_to_send())
                            else:
                                # PING requires eight payload bytes; send seven.
                                sock.sendall(
                                    b"\x00\x00\x07\x06\x00\x00\x00\x00\x00invalid"
                                )
                            event_sent.set()
                        # Keep TCP open: the client must act on the HTTP/2 event.
                        try:
                            while sock.recv(65536):
                                pass
                        except ConnectionResetError:
                            pass
                        except ssl.SSLError as error:
                            # Older Python can expose ragged EOF as a generic SSLError.
                            if (
                                not isinstance(error, ssl.SSLEOFError)
                                and "unexpected eof" not in str(error).lower()
                            ):
                                raise

        self._start_server(handler)
        with PoolManager(
            ssl_backend="ssl",
            ca_certs=DEFAULT_CA,
            timeout=5,
            disabled_svn={HttpVersion.h3},
            background_watch_delay=None,
            retries=False,
        ) as manager:
            url = f"https://{self.host}:{self.port}/"
            first = manager.urlopen("GET", url, preload_content=False)
            conn = first.connection
            assert first.version == 20 and conn is not None
            previous = conn.sock
            assert previous is not None
            assert first.read() == b"ok"
            send_event.set()
            assert event_sent.wait(5)
            assert wait_for_read(previous, timeout=5)
            second = manager.urlopen("GET", url, preload_content=False)
            assert second.version == 20 and second.connection is not None
            assert previous.fileno() == -1
            assert second.connection.sock is not previous
            assert second.read() == b"ok"

    def test_recovery_when_server_closes_connection(self) -> None:
        # Does the pool work seamlessly if an open connection in the
        # connection pool gets hung up on by the server, then reaches
        # the front of the queue again?

        done_closing = Event()

        def socket_handler(listener: socket.socket) -> None:
            for i in 0, 1:
                sock = listener.accept()[0]

                buf = b""
                while not buf.endswith(b"\r\n\r\n"):
                    buf = sock.recv(65536)

                body = f"Response {int(i)}"
                sock.send(
                    (
                        "HTTP/1.1 200 OK\r\n"
                        "Content-Type: text/plain\r\n"
                        "Content-Length: %d\r\n"
                        "\r\n"
                        "%s" % (len(body), body)
                    ).encode("utf-8")
                )

                sock.close()  # simulate a server timing out, closing socket
                done_closing.set()  # let the test know it can proceed

        self._start_server(socket_handler)
        with HTTPConnectionPool(self.host, self.port) as pool:
            response = pool.request("GET", "/", retries=0)
            assert response.status == 200
            assert response.data == b"Response 0"

            done_closing.wait()  # wait until the socket in our pool gets closed

            response = pool.request("GET", "/", retries=0)
            assert response.status == 200
            assert response.data == b"Response 1"

    def test_connection_refused(self) -> None:
        # Does the pool retry if there is no listener on the port?
        host, port = get_unreachable_address()
        with HTTPConnectionPool(host, port, maxsize=3, block=True) as http:
            with pytest.raises(MaxRetryError):
                http.request("GET", "/", retries=0, release_conn=False)
            assert http.pool is not None
            assert http.pool.qsize() == 0

    def test_connection_read_timeout(self) -> None:
        timed_out = Event()

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            while not sock.recv(65536).endswith(b"\r\n\r\n"):
                pass

            timed_out.wait()
            sock.close()

        self._start_server(socket_handler)
        with HTTPConnectionPool(
            self.host,
            self.port,
            timeout=SHORT_TIMEOUT,
            retries=False,
            maxsize=3,
            block=True,
        ) as http:
            try:
                with pytest.raises(ReadTimeoutError):
                    http.request("GET", "/", release_conn=False)
            finally:
                timed_out.set()

            assert http.pool is not None
            assert http.pool.qsize() == 0

    def test_read_timeout_dont_retry_method_not_in_allowlist(self) -> None:
        timed_out = Event()

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            sock.recv(65536)
            timed_out.wait()
            sock.close()

        self._start_server(socket_handler)
        with HTTPConnectionPool(
            self.host, self.port, timeout=LONG_TIMEOUT, retries=True
        ) as pool:
            try:
                with pytest.raises(ReadTimeoutError):
                    pool.request("POST", "/")
            finally:
                timed_out.set()

    def test_https_connection_read_timeout(self) -> None:
        """Handshake timeouts should fail with a Timeout"""
        timed_out = Event()

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            while not sock.recv(65536):
                pass

            timed_out.wait()
            sock.close()

        # first ReadTimeoutError due to SocketTimeout
        self._start_server(socket_handler)
        with HTTPSConnectionPool(
            self.host, self.port, timeout=LONG_TIMEOUT, retries=False
        ) as pool:
            try:
                with pytest.raises(ReadTimeoutError):
                    pool.request("GET", "/")
            finally:
                timed_out.set()

        # second ReadTimeoutError due to errno
        with HTTPSConnectionPool(host=self.host):
            err = OSError()
            err.errno = errno.EAGAIN
            with pytest.raises(ReadTimeoutError):
                pool._raise_timeout(err, "", 0)

    def test_timeout_errors_cause_retries(self) -> None:
        def socket_handler(listener: socket.socket) -> None:
            sock_timeout = listener.accept()[0]

            # Wait for a second request before closing the first socket.
            sock = listener.accept()[0]
            sock_timeout.close()

            # Second request.
            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += sock.recv(65536)

            # Now respond immediately.
            body = "Response 2"
            sock.send(
                (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/plain\r\n"
                    "Content-Length: %d\r\n"
                    "\r\n"
                    "%s" % (len(body), body)
                ).encode("utf-8")
            )

            sock.close()

        # In situations where the main thread throws an exception, the server
        # thread can hang on an accept() call. This ensures everything times
        # out within 3 second. This should be long enough for any socket
        # operations in the test suite to complete
        default_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(3)

        try:
            self._start_server(socket_handler)
            t = Timeout(connect=LONG_TIMEOUT, read=LONG_TIMEOUT)
            with HTTPConnectionPool(self.host, self.port, timeout=t) as pool:
                response = pool.request("GET", "/", retries=1)
                assert response.status == 200
                assert response.data == b"Response 2"
        finally:
            socket.setdefaulttimeout(default_timeout)

    def test_delayed_body_read_timeout(self) -> None:
        timed_out = Event()

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            buf = b""
            body = "Hi"
            while not buf.endswith(b"\r\n\r\n"):
                buf = sock.recv(65536)
            sock.send(
                (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/plain\r\n"
                    "Content-Length: %d\r\n"
                    "\r\n" % len(body)
                ).encode("utf-8")
            )

            timed_out.wait()
            sock.send(body.encode("utf-8"))
            sock.close()

        self._start_server(socket_handler)
        with HTTPConnectionPool(self.host, self.port) as pool:
            response = pool.urlopen(
                "GET",
                "/",
                retries=0,
                preload_content=False,
                timeout=Timeout(connect=1, read=LONG_TIMEOUT),
            )
            try:
                with pytest.raises(ReadTimeoutError):
                    response.read()
            finally:
                timed_out.set()

    def test_delayed_body_read_timeout_with_preload(self) -> None:
        timed_out = Event()

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            buf = b""
            body = "Hi"
            while not buf.endswith(b"\r\n\r\n"):
                buf += sock.recv(65536)
            sock.send(
                (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/plain\r\n"
                    "Content-Length: %d\r\n"
                    "\r\n" % len(body)
                ).encode("utf-8")
            )

            timed_out.wait(5)
            sock.close()

        self._start_server(socket_handler)
        with HTTPConnectionPool(self.host, self.port) as pool:
            try:
                with pytest.raises(ReadTimeoutError):
                    timeout = Timeout(connect=LONG_TIMEOUT, read=SHORT_TIMEOUT)
                    pool.urlopen("GET", "/", retries=False, timeout=timeout)
            finally:
                timed_out.set()

    def test_incomplete_response(self) -> None:
        body = "Response"
        partial_body = body[:2]

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            # Consume request
            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf = sock.recv(65536)

            # Send partial response and close socket.
            sock.send(
                (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/plain\r\n"
                    "Content-Length: %d\r\n"
                    "\r\n"
                    "%s" % (len(body), partial_body)
                ).encode("utf-8")
            )
            sock.close()

        self._start_server(socket_handler)
        with HTTPConnectionPool(self.host, self.port) as pool:
            response = pool.request("GET", "/", retries=0, preload_content=False)
            with pytest.raises(ProtocolError):
                response.read()

    def test_connection_cleanup_on_read_timeout(self) -> None:
        timed_out = Event()

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            buf = b""
            body = "Hi"
            while not buf.endswith(b"\r\n\r\n"):
                buf = sock.recv(65536)
            sock.send(
                (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/plain\r\n"
                    "Content-Length: %d\r\n"
                    "\r\n" % len(body)
                ).encode("utf-8")
            )

            timed_out.wait()
            sock.close()

        self._start_server(socket_handler)
        with HTTPConnectionPool(self.host, self.port) as pool:
            assert pool.pool is not None
            assert 0 == pool.pool.qsize()
            response = pool.urlopen(
                "GET", "/", retries=0, preload_content=False, timeout=LONG_TIMEOUT
            )
            assert (
                1 == pool.pool.qsize()
            )  # we always put the conn back now (since v2.12.907)
            try:
                with pytest.raises(ReadTimeoutError):
                    response.read()
                assert 1 == pool.pool.qsize()
            finally:
                timed_out.set()

    def test_invalid_http_status(self) -> None:
        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            # Consume request
            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf = sock.recv(65536)

            # Send partial response and close socket.
            sock.send(b"HTTP/1.1   OK\r\n\r\n")
            sock.close()

        self._start_server(socket_handler)

        with HTTPConnectionPool(self.host, self.port) as pool:
            with pytest.raises(ProtocolError):
                pool.request("GET", "/", retries=False)

    def test_invalid_incoming_header(self) -> None:
        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            # Consume request
            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf = sock.recv(65536)

            # Send partial response and close socket.
            sock.send(b"HTTP/1.1 200 OK\r\nX Invalid-Header: Value\r\n\r\n")
            sock.close()

        self._start_server(socket_handler)

        with HTTPConnectionPool(self.host, self.port) as pool:
            with pytest.raises(InvalidHeader):
                pool.request("GET", "/", retries=False)

    def test_connection_cleanup_on_protocol_error_during_read(self) -> None:
        body = "Response"
        partial_body = body[:2]

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            # Consume request
            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf = sock.recv(65536)

            # Send partial response and close socket.
            sock.send(
                (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/plain\r\n"
                    "Content-Length: %d\r\n"
                    "\r\n"
                    "%s" % (len(body), partial_body)
                ).encode("utf-8")
            )
            sock.close()

        self._start_server(socket_handler)
        with HTTPConnectionPool(self.host, self.port) as pool:
            assert pool.pool is not None
            assert 0 == pool.pool.qsize()
            response = pool.request("GET", "/", retries=0, preload_content=False)
            assert (
                1 == pool.pool.qsize()
            )  # we always put the conn back now (since v2.12.907)

            with pytest.raises(ProtocolError):
                response.read()
            assert 1 == pool.pool.qsize()

    def test_connection_closed_on_read_timeout_preload_false(self) -> None:
        timed_out = Event()

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            # Consume request
            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf = sock.recv(65535)

            # Send partial chunked response and then hang.
            sock.send(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"\r\n"
                b"8\r\n"
                b"12345678\r\n"
            )
            timed_out.wait(5)

            # Expect a new request, but keep hold of the old socket to avoid
            # leaking it. Because we don't want to hang this thread, we
            # actually use select.select to confirm that a new request is
            # coming in: this lets us time the thread out.
            rlist, _, _ = select.select([listener], [], [], 1)
            assert rlist
            new_sock = listener.accept()[0]

            # Consume request
            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf = new_sock.recv(65535)

            # Send complete chunked response.
            new_sock.send(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"\r\n"
                b"8\r\n"
                b"12345678\r\n"
                b"0\r\n\r\n"
            )

            new_sock.close()
            sock.close()

        self._start_server(socket_handler)
        with HTTPConnectionPool(self.host, self.port) as pool:
            # First request should fail.
            response = pool.urlopen(
                "GET", "/", retries=0, preload_content=False, timeout=LONG_TIMEOUT
            )
            try:
                with pytest.raises(ReadTimeoutError):
                    response.read()
            finally:
                timed_out.set()

            # Second should succeed.
            response = pool.urlopen(
                "GET", "/", retries=0, preload_content=False, timeout=LONG_TIMEOUT
            )
            assert len(response.read()) == 8

    def test_closing_response_actually_closes_connection(self) -> None:
        done_closing = Event()
        complete = Event()

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf = sock.recv(65536)

            sock.send(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 0\r\n"
                b"\r\n"
            )

            # Wait for the socket to close.
            done_closing.wait(timeout=LONG_TIMEOUT)

            # Look for the empty string to show that the connection got closed.
            # Don't get stuck in a timeout.
            sock.settimeout(LONG_TIMEOUT)
            new_data = sock.recv(65536)
            assert not new_data
            sock.close()
            complete.set()

        self._start_server(socket_handler)
        with HTTPConnectionPool(self.host, self.port) as pool:
            response = pool.request("GET", "/", retries=0, preload_content=False)
            assert response.status == 200
            response.close()

            done_closing.set()  # wait until the socket in our pool gets closed
            successful = complete.wait(timeout=LONG_TIMEOUT)
            assert successful, "Timed out waiting for connection close"

    def test_release_conn_param_is_respected_after_timeout_retry(self) -> None:
        """For successful ```urlopen(release_conn=False)```,
        the connection isn't released, even after a retry.

        This test allows a retry: one request fails, the next request succeeds.

        This is a regression test for issue #651 [1], where the connection
        would be released if the initial request failed, even if a retry
        succeeded.

        [1] <https://github.com/urllib3/urllib3/issues/651>
        """

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            consume_socket(sock)

            # Close the connection, without sending any response (not even the
            # HTTP status line). This will trigger a `Timeout` on the client,
            # inside `urlopen()`.
            sock.close()

            # Expect a new request. Because we don't want to hang this thread,
            # we actually use select.select to confirm that a new request is
            # coming in: this lets us time the thread out.
            rlist, _, _ = select.select([listener], [], [], 5)
            assert rlist
            sock = listener.accept()[0]
            consume_socket(sock)

            # Send complete chunked response.
            sock.send(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"\r\n"
                b"8\r\n"
                b"12345678\r\n"
                b"0\r\n\r\n"
            )

            sock.close()

        self._start_server(socket_handler)
        with HTTPConnectionPool(self.host, self.port, maxsize=1) as pool:
            # First request should fail, but the timeout and `retries=1` should
            # save it.
            response = pool.urlopen(
                "GET",
                "/",
                retries=1,
                release_conn=False,
                preload_content=False,
                timeout=LONG_TIMEOUT,
            )

            # The connection should still be on the response object, and none
            # should be in the pool. We opened two though.
            assert pool.num_connections == 2
            assert pool.pool is not None
            assert pool.pool.qsize() == 0
            assert response.connection is not None

            # Consume the data. This should put the connection back.
            response.read()
            assert pool.pool.qsize() == 1
            assert response.connection is None

    def test_socket_close_socket_then_file(self) -> None:
        def consume_ssl_socket(listener: socket.socket) -> None:
            try:
                with listener.accept()[0] as sock, original_ssl_wrap_socket(
                    sock,
                    server_side=True,
                    keyfile=DEFAULT_CERTS["keyfile"],
                    certfile=DEFAULT_CERTS["certfile"],
                    ca_certs=DEFAULT_CA,
                ) as ssl_sock:
                    consume_socket(ssl_sock)
            except (ConnectionResetError, ConnectionAbortedError, OSError):
                pass

        self._start_server(consume_ssl_socket)
        with socket.create_connection(
            (self.host, self.port)
        ) as sock, contextlib.closing(
            ssl_wrap_socket(sock, server_hostname=self.host, ca_certs=DEFAULT_CA)
        ) as ssl_sock, ssl_sock.makefile("rb") as f:
            ssl_sock.close()
            f.close()
            # SecureTransport is supposed to raise OSError but raises
            # ssl.SSLError when closed because ssl_sock.context is None
            with pytest.raises((OSError, ssl.SSLError)):
                ssl_sock.sendall(b"hello")
            assert ssl_sock.fileno() == -1

    def test_socket_close_stays_open_with_makefile_open(self) -> None:
        quit_event = threading.Event()

        def consume_ssl_socket(listener: socket.socket) -> None:
            try:
                with listener.accept()[0] as sock, original_ssl_wrap_socket(
                    sock,
                    server_side=True,
                    keyfile=DEFAULT_CERTS["keyfile"],
                    certfile=DEFAULT_CERTS["certfile"],
                    ca_certs=DEFAULT_CA,
                ):
                    quit_event.wait()
            except (ConnectionResetError, ConnectionAbortedError, OSError, SSLError):
                pass

        self._start_server(consume_ssl_socket)
        with socket.create_connection(
            (self.host, self.port)
        ) as sock, contextlib.closing(
            ssl_wrap_socket(sock, server_hostname=self.host, ca_certs=DEFAULT_CA)
        ) as ssl_sock, ssl_sock.makefile("rb"):
            ssl_sock.close()
            ssl_sock.close()
            ssl_sock.sendall(b"hello")
            assert ssl_sock.fileno() > 0
            quit_event.set()

    def test_socket_shutdown_stops_recv(self) -> None:
        """Backported from https://github.com/urllib3/urllib3/pull/3527 urllib3-future was
        already ready to never hang. we just want to be sure shutdown is there even
        if it does nothing."""
        timed_out, starting_read = Event(), Event()

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            ssl_sock = original_ssl_wrap_socket(
                sock,
                server_side=True,
                keyfile=DEFAULT_CERTS["keyfile"],
                certfile=DEFAULT_CERTS["certfile"],
                ca_certs=DEFAULT_CA,
            )

            # Consume request
            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf = ssl_sock.recv(65535)

            # Send incomplete message (note Content-Length)
            ssl_sock.send(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 10\r\n"
                b"\r\n"
                b"Hi-"
            )
            timed_out.wait(5)
            ssl_sock.close()

        self._start_server(socket_handler)

        class TestClient(threading.Thread):
            def __init__(self, host: str, port: int) -> None:
                super().__init__()
                self.host, self.port = host, port
                self.response: HTTPResponse | None = None

            def run(self) -> None:
                with HTTPSConnectionPool(
                    self.host, self.port, ca_certs=DEFAULT_CA
                ) as pool:
                    self.response = pool.urlopen(
                        "GET", "/", preload_content=False, retries=0
                    )
                    # either exception is accepted. the goal there is that we never
                    # hang.
                    with pytest.raises(
                        ProtocolError, match="Connection broken|IncompleteRead"
                    ):
                        starting_read.set()
                        self.response.read()

        test_client = TestClient(self.host, self.port)
        test_client.start()
        # First, wait to make sure the client is really stuck reading
        starting_read.wait(5)
        time.sleep(LONG_TIMEOUT)
        # Calling shutdown here calls shutdown() on the underlying socket,
        # so that the remaining read will fail instead of blocking
        # indefinitely
        assert test_client.response is not None
        test_client.response.shutdown()
        timed_out.set()


class TestProxyManager(SocketDummyServerTestCase):
    @pytest.mark.asyncio
    @pytest.mark.parametrize("asynchronous", [False, True])
    @pytest.mark.parametrize("remove_sensitive", [False, True])
    @pytest.mark.parametrize("target", ["same", "default-port", "cross", "proxy"])
    async def test_redirect_rewrites_host_and_strips_credentials(
        self, asynchronous: bool, remove_sensitive: bool, target: str
    ) -> None:
        from urllib3 import AsyncProxyManager

        requests: list[bytearray] = []

        def handler(listener: socket.socket) -> None:
            listener.settimeout(10)
            for first in (True, False):
                with listener.accept()[0] as sock:
                    sock.settimeout(10)
                    requests.append(consume_socket(sock))
                    if first:
                        sock.sendall(
                            b"HTTP/1.1 302 Found\r\nLocation: "
                            + location.encode()
                            + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                        )
                    else:
                        sock.sendall(
                            b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n"
                            b"Connection: close\r\n\r\n"
                        )

        self._start_server(handler)
        proxy_url = f"http://{self.host}:{self.port}"
        location, expected_host = {
            "same": ("http://google.com/second", "google.com"),
            "default-port": ("http://google.com:80/second", "google.com"),
            "cross": ("http://example.com/second", "example.com"),
            "proxy": (proxy_url + "/second", f"{self.host}:{self.port}"),
        }[target]
        headers = {"Authorization": "Bearer secret", "Cookie": "private=yes"}
        retries = Retry(
            total=1,
            remove_headers_on_redirect=(
                Retry.DEFAULT_REMOVE_HEADERS_ON_REDIRECT if remove_sensitive else ()
            ),
        )
        if asynchronous:
            async with AsyncProxyManager(
                proxy_url, headers=headers, timeout=5, retries=retries
            ) as manager_async:
                response_async = await manager_async.request(
                    "GET", "http://google.com/first"
                )
                assert response_async.status == 200
        else:
            with proxy_from_url(
                proxy_url, headers=headers, timeout=5, retries=retries
            ) as manager:
                assert manager.request("GET", "http://google.com/first").status == 200

        assert len(requests) == 2
        assert requests[1].startswith(b"GET " + location.encode() + b" HTTP/1.1\r\n")
        received = HTTPHeaderDict()
        for line in requests[1].split(b"\r\n")[1:]:
            if line:
                name, value = line.decode().split(":", 1)
                received.add(name, value.strip())
        assert received["Host"] == expected_host
        retain_credentials = target in {"same", "default-port"} or not remove_sensitive
        assert ("Authorization" in received) is retain_credentials
        assert ("Cookie" in received) is retain_credentials
        assert headers == {"Authorization": "Bearer secret", "Cookie": "private=yes"}

    def test_simple(self) -> None:
        def echo_socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += sock.recv(65536)

            sock.send(
                (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/plain\r\n"
                    "Content-Length: %d\r\n"
                    "\r\n"
                    "%s" % (len(buf), buf.decode("utf-8"))
                ).encode("utf-8")
            )
            sock.close()

        self._start_server(echo_socket_handler)
        base_url = f"http://{self.host}:{self.port}"
        with proxy_from_url(base_url) as proxy:
            r = proxy.request("GET", "http://google.com/")

            assert r.status == 200
            # FIXME: The order of the headers is not predictable right now. We
            # should fix that someday (maybe when we migrate to
            # OrderedDict/MultiDict).
            assert sorted(r.data.split(b"\r\n")) == sorted(
                [
                    b"GET http://google.com/ HTTP/1.1",
                    b"Host: google.com",
                    b"Accept-Encoding: identity",
                    b"Accept: */*",
                    b"User-Agent: " + _get_default_user_agent().encode("utf-8"),
                    b"",
                    b"",
                ]
            )

    def test_proxy_tunnel_connect_nak(self) -> None:
        def echo_socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += sock.recv(65536)

            sock.send(b"HTTP/1.1 401 Unauthorized\r\n\r\n")
            sock.close()

        self._start_server(echo_socket_handler)
        base_url = f"http://{self.host}:{self.port}"

        with ProxyManager(base_url) as proxy:
            with pytest.raises(
                MaxRetryError, match=r"Tunnel connection failed: 401 Unauthorized"
            ):
                proxy.request("GET", "https://www.google.com/", retries=0)

    def test_headers(self) -> None:
        def echo_socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += sock.recv(65536)

            sock.send(
                (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/plain\r\n"
                    "Content-Length: %d\r\n"
                    "\r\n"
                    "%s" % (len(buf), buf.decode("utf-8"))
                ).encode("utf-8")
            )
            sock.close()

        self._start_server(echo_socket_handler)
        base_url = f"http://{self.host}:{self.port}"

        # Define some proxy headers.
        proxy_headers = HTTPHeaderDict({"For-The-Proxy": "YEAH!"})
        with proxy_from_url(base_url, proxy_headers=proxy_headers) as proxy:
            conn = proxy.connection_from_url("http://www.google.com/")

            r = conn.urlopen("GET", "http://www.google.com/", assert_same_host=False)

            assert r.status == 200
            # FIXME: The order of the headers is not predictable right now. We
            # should fix that someday (maybe when we migrate to
            # OrderedDict/MultiDict).
            assert b"For-The-Proxy: YEAH!\r\n" in r.data

    def test_retries(self) -> None:
        close_event = Event()

        def echo_socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            # First request, which should fail
            sock.close()

            # Second request
            sock = listener.accept()[0]

            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += sock.recv(65536)

            sock.send(
                (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/plain\r\n"
                    "Content-Length: %d\r\n"
                    "\r\n"
                    "%s" % (len(buf), buf.decode("utf-8"))
                ).encode("utf-8")
            )
            sock.close()
            close_event.set()

        self._start_server(echo_socket_handler)
        base_url = f"http://{self.host}:{self.port}"

        with proxy_from_url(base_url) as proxy:
            conn = proxy.connection_from_url("http://www.google.com")

            r = conn.urlopen(
                "GET", "http://www.google.com", assert_same_host=False, retries=1
            )
            assert r.status == 200

            close_event.wait(timeout=LONG_TIMEOUT)
            with pytest.raises(ProxyError):
                conn.urlopen(
                    "GET",
                    "http://www.google.com",
                    assert_same_host=False,
                    retries=False,
                )

    def test_connect_reconn(self) -> None:
        def proxy_ssl_one(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += sock.recv(65536)
            s = buf.decode("utf-8")
            if not s.startswith("CONNECT "):
                sock.send(b"HTTP/1.1 405 Method not allowed\r\nAllow: CONNECT\r\n\r\n")
                sock.close()
                return

            if not s.startswith(f"CONNECT {self.host}:443"):
                sock.send(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                sock.close()
                return

            sock.send(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            ssl_sock = original_ssl_wrap_socket(
                sock,
                server_side=True,
                keyfile=DEFAULT_CERTS["keyfile"],
                certfile=DEFAULT_CERTS["certfile"],
                ca_certs=DEFAULT_CA,
            )

            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += ssl_sock.recv(65536)

            ssl_sock.send(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 2\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                b"Hi"
            )
            ssl_sock.close()

        def echo_socket_handler(listener: socket.socket) -> None:
            proxy_ssl_one(listener)
            proxy_ssl_one(listener)

        self._start_server(echo_socket_handler)
        base_url = f"http://{self.host}:{self.port}"

        with proxy_from_url(base_url, ca_certs=DEFAULT_CA) as proxy:
            url = f"https://{self.host}"
            conn = proxy.connection_from_url(url)
            r = conn.urlopen("GET", url, retries=0)
            assert r.status == 200
            r = conn.urlopen("GET", url, retries=0)
            assert r.status == 200

    def test_connect_ipv6_addr(self) -> None:
        ipv6_addr = "2001:4998:c:a06::2:4008"

        def echo_socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += sock.recv(65536)
            s = buf.decode("utf-8")

            if s.startswith(f"CONNECT [{ipv6_addr}]:443"):
                sock.send(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                ssl_sock = original_ssl_wrap_socket(
                    sock,
                    server_side=True,
                    keyfile=DEFAULT_CERTS["keyfile"],
                    certfile=DEFAULT_CERTS["certfile"],
                )
                buf = b""
                while not buf.endswith(b"\r\n\r\n"):
                    buf += ssl_sock.recv(65536)

                ssl_sock.send(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/plain\r\n"
                    b"Content-Length: 2\r\n"
                    b"Connection: close\r\n"
                    b"\r\n"
                    b"Hi"
                )
                ssl_sock.close()
            else:
                sock.close()

        self._start_server(echo_socket_handler)
        base_url = f"http://{self.host}:{self.port}"

        with proxy_from_url(base_url, cert_reqs="NONE") as proxy:
            url = f"https://[{ipv6_addr}]"
            conn = proxy.connection_from_url(url)
            try:
                with pytest.warns(InsecureRequestWarning):
                    r = conn.urlopen("GET", url, retries=0)
                assert r.status == 200
            except MaxRetryError:
                pytest.fail("Invalid IPv6 format in HTTP CONNECT request")

    @pytest.mark.parametrize("target_scheme", ["http", "https"])
    def test_https_proxymanager_connected_to_http_proxy(
        self, target_scheme: str
    ) -> None:
        errored = Event()

        def http_socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            sock.send(b"HTTP/1.0 501 Not Implemented\r\nConnection: close\r\n\r\n")
            errored.wait()
            sock.close()

        self._start_server(http_socket_handler)
        base_url = f"https://{self.host}:{self.port}"

        with ProxyManager(base_url, cert_reqs="NONE") as proxy:
            with pytest.raises(MaxRetryError) as e:
                proxy.request("GET", f"{target_scheme}://example.com", retries=0)

            errored.set()  # Avoid a ConnectionAbortedError on Windows.

            assert type(e.value.reason) == ProxyError
            assert "Your proxy appears to only use HTTP and not HTTPS" in str(
                e.value.reason
            )


class TestSSL(SocketDummyServerTestCase):
    def test_ssl_failure_midway_through_conn(self) -> None:
        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            sock2 = sock.dup()
            ssl_sock = original_ssl_wrap_socket(
                sock,
                server_side=True,
                keyfile=DEFAULT_CERTS["keyfile"],
                certfile=DEFAULT_CERTS["certfile"],
                ca_certs=DEFAULT_CA,
            )

            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += ssl_sock.recv(65536)

            # Deliberately send from the non-SSL socket.
            sock2.send(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 2\r\n"
                b"\r\n"
                b"Hi"
            )
            sock2.close()
            ssl_sock.close()

        self._start_server(socket_handler)
        with HTTPSConnectionPool(self.host, self.port, ca_certs=DEFAULT_CA) as pool:
            with pytest.raises(
                SSLError,
                match=r"(wrong version number|record overflow|record layer failure|received corrupt message|SSL operation: error)",
            ):
                pool.request("GET", "/", retries=False)

    def test_ssl_read_timeout(self) -> None:
        timed_out = Event()

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            # disable Nagle's algorithm so there's no delay in sending a partial body
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, True)
            ssl_sock = original_ssl_wrap_socket(
                sock,
                server_side=True,
                keyfile=DEFAULT_CERTS["keyfile"],
                certfile=DEFAULT_CERTS["certfile"],
            )

            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += ssl_sock.recv(65536)

            # Send incomplete message (note Content-Length)
            ssl_sock.send(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 10\r\n"
                b"\r\n"
                b"Hi-"
            )
            timed_out.wait()

            sock.close()
            ssl_sock.close()

        self._start_server(socket_handler)
        with HTTPSConnectionPool(self.host, self.port, ca_certs=DEFAULT_CA) as pool:
            response = pool.urlopen(
                "GET", "/", retries=0, preload_content=False, timeout=LONG_TIMEOUT
            )
            try:
                with pytest.raises(ReadTimeoutError):
                    response.read()
            finally:
                timed_out.set()

    def test_ssl_failed_fingerprint_verification(self) -> None:
        def socket_handler(listener: socket.socket) -> None:
            for i in range(2):
                sock = listener.accept()[0]
                try:
                    ssl_sock = original_ssl_wrap_socket(
                        sock,
                        server_side=True,
                        keyfile=DEFAULT_CERTS["keyfile"],
                        certfile=DEFAULT_CERTS["certfile"],
                        ca_certs=DEFAULT_CA,
                    )
                except (ssl.SSLError, ConnectionResetError):
                    if i == 1:
                        raise
                    return

                ssl_sock.send(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/plain\r\n"
                    b"Content-Length: 5\r\n\r\n"
                    b"Hello"
                )

                ssl_sock.close()
                sock.close()

        self._start_server(socket_handler)
        # GitHub's fingerprint. Valid, but not matching.
        fingerprint = "A0:C4:A7:46:00:ED:A7:2D:C0:BE:CB:9A:8C:B6:07:CA:58:EE:74:5E"

        def request() -> None:
            pool = HTTPSConnectionPool(
                self.host, self.port, assert_fingerprint=fingerprint
            )
            try:
                timeout = Timeout(connect=LONG_TIMEOUT, read=SHORT_TIMEOUT)
                response = pool.urlopen(
                    "GET", "/", preload_content=False, retries=0, timeout=timeout
                )
                response.read()
            finally:
                pool.close()

        with pytest.raises(MaxRetryError) as cm:
            request()
        assert isinstance(cm.value.reason, SSLError)
        # Should not hang, see https://github.com/urllib3/urllib3/issues/529
        with pytest.raises(MaxRetryError):
            request()

    def test_retry_ssl_error(self) -> None:
        def socket_handler(listener: socket.socket) -> None:
            # first request, trigger an SSLError
            sock = listener.accept()[0]
            sock2 = sock.dup()
            ssl_sock = original_ssl_wrap_socket(
                sock,
                server_side=True,
                keyfile=DEFAULT_CERTS["keyfile"],
                certfile=DEFAULT_CERTS["certfile"],
            )
            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += ssl_sock.recv(65536)

            # Deliberately send from the non-SSL socket to trigger an SSLError
            sock2.send(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 4\r\n"
                b"\r\n"
                b"Fail"
            )
            sock2.close()
            ssl_sock.close()

            # retried request
            sock = listener.accept()[0]
            ssl_sock = original_ssl_wrap_socket(
                sock,
                server_side=True,
                keyfile=DEFAULT_CERTS["keyfile"],
                certfile=DEFAULT_CERTS["certfile"],
            )
            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += ssl_sock.recv(65536)
            ssl_sock.send(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 7\r\n\r\n"
                b"Success"
            )
            ssl_sock.close()

        self._start_server(socket_handler)

        with HTTPSConnectionPool(self.host, self.port, ca_certs=DEFAULT_CA) as pool:
            response = pool.urlopen("GET", "/", retries=1)
            assert response.data == b"Success"

    def test_ssl_load_default_certs_when_empty(self) -> None:
        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            try:
                ssl_sock = original_ssl_wrap_socket(
                    sock,
                    server_side=True,
                    keyfile=DEFAULT_CERTS["keyfile"],
                    certfile=DEFAULT_CERTS["certfile"],
                    ca_certs=DEFAULT_CA,
                )
            except (ssl.SSLError, OSError):
                return

            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += ssl_sock.recv(65536)

            ssl_sock.send(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 5\r\n\r\n"
                b"Hello"
            )

            ssl_sock.close()
            sock.close()

        context = mock.create_autospec(ssl_.SSLContext)
        context.load_default_certs = mock.Mock()
        context.wrap_socket.side_effect = ssl.SSLError("intentional test failure")
        context.options = 0

        from urllib3.util.ssl_ import _SSLContextCache

        _SSLContextCache.clear()

        try:
            with mock.patch("urllib3.util.ssl_.SSLContext", lambda *_, **__: context):
                self._start_server(socket_handler)
                with HTTPSConnectionPool(self.host, self.port) as pool:
                    # Without a proper `SSLContext`, this request will fail in some
                    # arbitrary way, but we only want to know if load_default_certs() was
                    # called, which is why we accept any `Exception` here.
                    with pytest.raises(Exception):
                        pool.request("GET", "/", timeout=SHORT_TIMEOUT)
                    context.load_default_certs.assert_called_with()
        finally:
            _SSLContextCache.clear()

    def test_ssl_ctx_need_convert_nonstdlib(self, monkeypatch) -> None:  # type: ignore
        def forbidden(ctx, *a, **kw):  # type: ignore
            raise AssertionError(
                "load_default_certs must not be called in this test run"
            )

        from urllib3.contrib.anytls import ssl as alt_ssl, IS_NONSTDLIB

        if not IS_NONSTDLIB:
            pytest.skip("Test requires a non-stdlib ssl backend (rtls/utls)")

        monkeypatch.setattr(ssl.SSLContext, "load_default_certs", forbidden)
        monkeypatch.setattr(alt_ssl.SSLContext, "load_default_certs", forbidden)

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            try:
                ssl_sock = original_ssl_wrap_socket(
                    sock,
                    server_side=True,
                    keyfile=DEFAULT_CERTS["keyfile"],
                    certfile=DEFAULT_CERTS["certfile"],
                    ca_certs=DEFAULT_CA,
                )
            except (ssl.SSLError, OSError):
                return

            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += ssl_sock.recv(65536)

            ssl_sock.send(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 5\r\n\r\n"
                b"Hello"
            )

            ssl_sock.close()
            sock.close()

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cafile=DEFAULT_CA)

        with pytest.raises(AssertionError):
            context.load_default_certs()

        from urllib3.util.ssl_ import _SSLContextCache

        _SSLContextCache.clear()

        self._start_server(socket_handler)

        with HTTPSConnectionPool(
            self.host, self.port, retries=False, ssl_context=context
        ) as pool:
            r = pool.request("GET", "/", timeout=LONG_TIMEOUT)
            assert r.status == 200

    def test_ssl_dont_load_default_certs_when_given(self, monkeypatch) -> None:  # type: ignore
        def forbidden(ctx, *a, **kw):  # type: ignore
            raise AssertionError(
                "load_default_certs must not be called in this test run"
            )

        from urllib3.contrib.anytls import ssl as anytls_ssl, IS_NONSTDLIB

        alt_ssl = anytls_ssl if IS_NONSTDLIB else None

        if alt_ssl is None:
            monkeypatch.setattr(ssl.SSLContext, "load_default_certs", forbidden)
        else:
            monkeypatch.setattr(alt_ssl.SSLContext, "load_default_certs", forbidden)

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            try:
                ssl_sock = original_ssl_wrap_socket(
                    sock,
                    server_side=True,
                    keyfile=DEFAULT_CERTS["keyfile"],
                    certfile=DEFAULT_CERTS["certfile"],
                    ca_certs=DEFAULT_CA,
                )
            except (ssl.SSLError, OSError):
                return

            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += ssl_sock.recv(65536)

            ssl_sock.send(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 5\r\n\r\n"
                b"Hello"
            )

            ssl_sock.close()
            sock.close()

        if alt_ssl is not None:
            context = alt_ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        else:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cafile=DEFAULT_CA)

        with pytest.raises(AssertionError):
            context.load_default_certs()

        for kwargs in [
            {"ca_certs": DEFAULT_CA},
            {"ssl_context": context},
        ]:
            from urllib3.util.ssl_ import _SSLContextCache

            _SSLContextCache.clear()

            self._start_server(socket_handler)

            with HTTPSConnectionPool(
                self.host, self.port, retries=False, **kwargs
            ) as pool:
                r = pool.request("GET", "/", timeout=LONG_TIMEOUT)
                assert r.status == 200

    def test_load_verify_locations_exception(self) -> None:
        """
        Ensure that load_verify_locations raises SSLError for all backends
        """
        with pytest.raises(SSLError):
            ssl_wrap_socket(None, ca_certs="/tmp/fake-file")  # type: ignore[call-overload]

    def test_ssl_custom_validation_failure_terminates(self, tmpdir: Path) -> None:
        """
        Ensure that the underlying socket is terminated if custom validation fails.
        """
        server_closed = Event()

        def is_closed_socket(sock: socket.socket) -> bool:
            try:
                sock.settimeout(SHORT_TIMEOUT)
            except OSError:
                return True
            return False

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            try:
                _ = original_ssl_wrap_socket(
                    sock,
                    server_side=True,
                    keyfile=DEFAULT_CERTS["keyfile"],
                    certfile=DEFAULT_CERTS["certfile"],
                    ca_certs=DEFAULT_CA,
                )
            except ConnectionResetError:
                # Windows can report the rejected handshake as a TCP reset.
                server_closed.set()
                return
            except ssl.SSLError as e:
                assert "alert unknown ca" in str(e) or "UnknownIssuer" in str(e)
                if is_closed_socket(sock):
                    server_closed.set()

        self._start_server(socket_handler)

        # client uses a different ca
        other_ca = trustme.CA()
        other_ca_path = str(tmpdir / "ca.pem")
        other_ca.cert_pem.write_to_path(other_ca_path)

        with HTTPSConnectionPool(
            self.host, self.port, cert_reqs="REQUIRED", ca_certs=other_ca_path
        ) as pool:
            with pytest.raises(SSLError):
                pool.request("GET", "/", retries=False, timeout=LONG_TIMEOUT)
        assert server_closed.wait(LONG_TIMEOUT), "The socket was not terminated"

    @pytest.mark.skipif(
        os.environ.get("CI") is not None,
        reason="too slow to run in CI",
    )
    @pytest.mark.parametrize(
        "preload_content,read_amt", [(True, None), (False, None), (False, 2**31)]
    )
    def test_requesting_large_resources_via_ssl(
        self, preload_content: bool, read_amt: int | None
    ) -> None:
        """
        Ensure that it is possible to read 2 GiB or more via an SSL
        socket.
        https://github.com/urllib3/urllib3/issues/2513
        """
        content_length = 2**31  # (`int` max value in C) + 1.
        ssl_ready = Event()

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            ssl_sock = original_ssl_wrap_socket(
                sock,
                server_side=True,
                keyfile=DEFAULT_CERTS["keyfile"],
                certfile=DEFAULT_CERTS["certfile"],
                ca_certs=DEFAULT_CA,
            )
            ssl_ready.set()

            while not ssl_sock.recv(65536).endswith(b"\r\n\r\n"):
                continue

            ssl_sock.send(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: %d\r\n\r\n" % content_length
            )

            chunks = 2
            for i in range(chunks):
                ssl_sock.sendall(bytes(content_length // chunks))

            ssl_sock.close()
            sock.close()

        self._start_server(socket_handler)
        ssl_ready.wait(5)
        with HTTPSConnectionPool(
            self.host, self.port, ca_certs=DEFAULT_CA, retries=False
        ) as pool:
            response = pool.request("GET", "/", preload_content=preload_content)
            data = response.data if preload_content else response.read(read_amt)
            assert len(data) == content_length


class TestErrorWrapping(SocketDummyServerTestCase):
    def test_bad_statusline(self) -> None:
        self.start_response_handler(
            b"HTTP/1.1 Omg What Is This?\r\nContent-Length: 0\r\n\r\n"
        )
        with HTTPConnectionPool(self.host, self.port, retries=False) as pool:
            with pytest.raises(ProtocolError):
                pool.request("GET", "/")

    def test_unknown_protocol(self) -> None:
        self.start_response_handler(b"HTTP/1000 200 OK\r\nContent-Length: 0\r\n\r\n")
        with HTTPConnectionPool(self.host, self.port, retries=False) as pool:
            with pytest.raises(ProtocolError):
                pool.request("GET", "/")


class TestInformationalResponse(SocketDummyServerTestCase):
    """
    HTTP/1.1 tests for Informational Responses!
    """

    def test_early_hint_catch(self) -> None:
        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += sock.recv(65536)

            sock.send(
                b"HTTP/1.1 103 Early Hints\r\n"
                + b"\r\n".join(
                    [
                        (k.encode("utf8") + b": " + v.encode("utf8"))
                        for (k, v) in [
                            (
                                "link",
                                "<https://cdn.example.com>; rel=preconnect, <https://cdn.example.com>; rel=preconnect; crossorigin",
                            ),
                        ]
                    ]
                )
                + b"\r\n\r\n"
            )

            sock.send(
                b"HTTP/1.1 200 OK\r\n"
                + b"\r\n".join(
                    [
                        (k.encode("utf8") + b": " + v.encode("utf8"))
                        for (k, v) in [("hello", "bar"), ("x-testing", "p")]
                    ]
                )
                + b"\r\n\r\n"
            )
            sock.close()

        self._start_server(socket_handler)

        early_hint_received = False

        def my_dumb_callback(early_response: HTTPResponse) -> None:
            nonlocal early_hint_received

            assert early_response.status == 103
            assert "link" in early_response.headers

            early_hint_received = True

        with HTTPConnectionPool(self.host, self.port, retries=False) as pool:
            r = pool.request("GET", "/", on_early_response=my_dumb_callback)

            assert r.status == 200
            assert r.headers
            assert "x-testing" in r.headers

        assert early_hint_received is True

    def test_processing_many_ping_deprecated(self) -> None:
        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += sock.recv(65536)

            sock.send(
                b"HTTP/1.1 102 Processing\r\n"
                + b"\r\n".join(
                    [
                        (k.encode("utf8") + b": " + v.encode("utf8"))
                        for (k, v) in [
                            ("x-progress", "10"),
                        ]
                    ]
                )
                + b"\r\n\r\n"
            )

            sock.send(
                b"HTTP/1.1 102 Processing\r\n"
                + b"\r\n".join(
                    [
                        (k.encode("utf8") + b": " + v.encode("utf8"))
                        for (k, v) in [
                            ("x-progress", "88"),
                        ]
                    ]
                )
                + b"\r\n\r\n"
            )

            sock.send(
                b"HTTP/1.1 102 Processing\r\n"
                + b"\r\n".join(
                    [
                        (k.encode("utf8") + b": " + v.encode("utf8"))
                        for (k, v) in [
                            ("x-progress", "99"),
                        ]
                    ]
                )
                + b"\r\n\r\n"
            )

            sock.send(
                b"HTTP/1.1 200 OK\r\n"
                + b"\r\n".join(
                    [
                        (k.encode("utf8") + b": " + v.encode("utf8"))
                        for (k, v) in [("hello", "bar"), ("x-testing", "p")]
                    ]
                )
                + b"\r\n\r\n"
            )
            sock.close()

        self._start_server(socket_handler)

        early_response_count = 0

        def my_dumb_callback(early_response: HTTPResponse) -> None:
            nonlocal early_response_count

            assert early_response.status == 102
            early_response_count += 1

        with HTTPConnectionPool(self.host, self.port, retries=False) as pool:
            r = pool.request("GET", "/", on_early_response=my_dumb_callback)

            assert r.status == 200
            assert r.headers
            assert "x-testing" in r.headers

        assert early_response_count == 3

    def test_switching_protocol_is_not_early_response(self) -> None:
        mutual_close_agreement = threading.Event()

        def socket_handler(listener: socket.socket) -> None:
            nonlocal mutual_close_agreement
            sock = listener.accept()[0]

            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += sock.recv(65536)

            sock.send(b"HTTP/1.1 101 Switching Protocols\r\n" + b"\r\n\r\n")
            mutual_close_agreement.wait()
            sock.close()

        self._start_server(socket_handler)

        early_response_received = False

        def my_dumb_callback(early_response: HTTPResponse) -> None:
            nonlocal early_response_received
            early_response_received = True

        with HTTPConnectionPool(self.host, self.port, retries=False) as pool:
            r = pool.request(
                "GET",
                "/",
                headers={"Upgrade": "websocket"},
                on_early_response=my_dumb_callback,
            )

            assert r.status == 101
            assert r.extension is not None
            r.extension.close()
            mutual_close_agreement.set()

        assert early_response_received is False


class TestHeaders(SocketDummyServerTestCase):
    def start_parsing_handler(self) -> None:
        self.parsed_headers: typing.OrderedDict[str, str] = OrderedDict()
        self.received_headers: list[bytes] = []

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += sock.recv(65536)

            self.received_headers = [
                header for header in buf.split(b"\r\n")[1:] if header
            ]

            for header in self.received_headers:
                (key, value) = header.split(b": ")
                self.parsed_headers[key.decode("ascii")] = value.decode("ascii")

            sock.send(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")

            sock.close()

        self._start_server(socket_handler)

    def test_ua_header_can_be_overridden(self) -> None:
        headers = {"User-Agent": "Definitely not urllib3!"}

        self.start_parsing_handler()
        expected_headers = {
            "Accept-Encoding": "identity",
            "Host": f"{self.host}:{self.port}",
        }
        expected_headers.update(headers)

        with HTTPConnectionPool(self.host, self.port, retries=False) as pool:
            pool.request("GET", "/", headers=HTTPHeaderDict(headers))
            assert expected_headers == self.parsed_headers

    def test_request_headers_are_sent_in_the_original_order(self) -> None:
        # NOTE: Probability this test gives a false negative is 1/(K!)
        K = 16
        # NOTE: Provide headers in non-sorted order (i.e. reversed)
        #       so that if the internal implementation tries to sort them,
        #       a change will be detected.
        expected_request_headers = [
            (f"X-Header-{int(i)}", str(i)) for i in reversed(range(K))
        ]

        def filter_non_x_headers(
            d: typing.OrderedDict[str, str],
        ) -> list[tuple[str, str]]:
            return [(k, v) for (k, v) in d.items() if k.startswith("X-Header-")]

        self.start_parsing_handler()

        with HTTPConnectionPool(self.host, self.port, retries=False) as pool:
            pool.request("GET", "/", headers=OrderedDict(expected_request_headers))
            assert expected_request_headers == filter_non_x_headers(self.parsed_headers)

    @resolvesLocalhostFQDN()
    def test_request_host_header_ignores_fqdn_dot(self) -> None:
        self.start_parsing_handler()

        with HTTPConnectionPool(self.host + ".", self.port, retries=False) as pool:
            pool.request("GET", "/")
            self.assert_header_received(
                self.received_headers, "Host", f"{self.host}:{self.port}"
            )

    def test_response_headers_are_returned_in_the_original_order(self) -> None:
        # NOTE: Probability this test gives a false negative is 1/(K!)
        K = 16
        # NOTE: Provide headers in non-sorted order (i.e. reversed)
        #       so that if the internal implementation tries to sort them,
        #       a change will be detected.
        expected_response_headers = [
            (f"x-header-{int(i)}", str(i)) for i in reversed(range(K))
        ]

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += sock.recv(65536)

            sock.send(
                b"HTTP/1.1 200 OK\r\n"
                + b"\r\n".join(
                    [
                        (k.encode("utf8") + b": " + v.encode("utf8"))
                        for (k, v) in expected_response_headers
                    ]
                )
                + b"\r\n\r\n"
            )
            sock.close()

        self._start_server(socket_handler)
        with HTTPConnectionPool(self.host, self.port) as pool:
            r = pool.request("GET", "/", retries=0)
            actual_response_headers = [
                (k, v)
                for (k, v) in r.headers.items()
                if k.lower().startswith("x-header-")
            ]
            assert expected_response_headers == actual_response_headers

    @pytest.mark.parametrize(
        "method_type, body_type",
        [
            ("GET", None),
            ("POST", None),
            ("POST", "bytes"),
            ("POST", "bytes-io"),
        ],
    )
    def test_headers_sent_with_add(
        self, method_type: str, body_type: str | None
    ) -> None:
        """
        Confirm that when adding headers with combine=True that we simply append to the
        most recent value, rather than create a new header line.
        """
        body: None | bytes | io.BytesIO
        if body_type is None:
            body = None
        elif body_type == "bytes":
            body = b"my-body"
        elif body_type == "bytes-io":
            body = io.BytesIO(b"bytes-io-body")
            body.seek(0, 0)
        else:
            raise ValueError("Unknown body type")

        buffer: bytes = b""

        def socket_handler(listener: socket.socket) -> None:
            nonlocal buffer
            sock = listener.accept()[0]
            sock.settimeout(0)

            start = time.time()
            while time.time() - start < (LONG_TIMEOUT / 2):
                try:
                    buffer += sock.recv(65536)
                except OSError:
                    continue

            sock.sendall(
                b"HTTP/1.1 200 OK\r\nServer: example.com\r\nContent-Length: 0\r\n\r\n"
            )
            sock.close()

        self._start_server(socket_handler)

        headers = HTTPHeaderDict()
        headers.add("A", "1")
        headers.add("C", "3")
        headers.add("B", "2")
        headers.add("B", "3")
        headers.add("A", "4", combine=False)
        headers.add("C", "5", combine=True)
        headers.add("C", "6")

        with HTTPConnectionPool(self.host, self.port, retries=False) as pool:
            r = pool.request(
                method_type,
                "/",
                body=body,
                headers=headers,
            )
            assert r.status == 200
            assert b"A: 1\r\nA: 4\r\nC: 3, 5\r\nC: 6\r\nB: 2\r\nB: 3" in buffer


class TestBrokenHeaders(SocketDummyServerTestCase):
    def _test_broken_header_parsing(
        self, headers: list[bytes], unparsed_data_check: str | None = None
    ) -> None:
        self.start_response_handler(
            (b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nContent-type: text/plain\r\n")
            + b"\r\n".join(headers)
            + b"\r\n\r\n"
        )

        with HTTPConnectionPool(self.host, self.port, retries=False) as pool:
            with LogRecorder() as logs:
                pool.request("GET", "/")

            for record in logs:
                if (
                    "Failed to parse headers" in record.msg
                    and isinstance(record.args, tuple)
                    and _url_from_pool(pool, "/") == record.args[0]
                ):
                    if (
                        unparsed_data_check is None
                        or unparsed_data_check in record.getMessage()
                    ):
                        return
            pytest.fail("Missing log about unparsed headers")


class TestHeaderParsingContentType(SocketDummyServerTestCase):
    def _test_okay_header_parsing(self, header: bytes) -> None:
        self.start_response_handler(
            (b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n") + header + b"\r\n\r\n"
        )

        with HTTPConnectionPool(self.host, self.port, retries=False) as pool:
            with LogRecorder() as logs:
                pool.request("GET", "/")

            for record in logs:
                assert "Failed to parse headers" not in record.msg

    def test_header_text_plain(self) -> None:
        self._test_okay_header_parsing(b"Content-type: text/plain")

    def test_header_message_rfc822(self) -> None:
        self._test_okay_header_parsing(b"Content-type: message/rfc822")


class TestHEAD(SocketDummyServerTestCase):
    def test_chunked_head_response_does_not_hang(self) -> None:
        self.start_response_handler(
            b"HTTP/1.1 200 OK\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Content-type: text/plain\r\n"
            b"\r\n"
        )
        with HTTPConnectionPool(self.host, self.port, retries=False) as pool:
            r = pool.request("HEAD", "/", timeout=LONG_TIMEOUT, preload_content=False)

            # stream will use the read_chunked method here.
            assert [] == list(r.stream())

    def test_empty_head_response_does_not_hang(self) -> None:
        self.start_response_handler(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 256\r\n"
            b"Content-type: text/plain\r\n"
            b"\r\n"
        )
        with HTTPConnectionPool(self.host, self.port, retries=False) as pool:
            r = pool.request("HEAD", "/", timeout=LONG_TIMEOUT, preload_content=False)

            # stream will use the read method here.
            assert [] == list(r.stream())


class TestStream(SocketDummyServerTestCase):
    def test_response_read_from_closed_descriptor(self) -> None:
        self.start_response_handler(b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\n")
        with HTTPConnectionPool(self.host, self.port, timeout=5, retries=False) as pool:
            response = pool.request("GET", "/", preload_content=False)
            try:
                conn = response.connection
                assert conn is not None and conn.sock is not None
                conn.sock.close()
                with pytest.raises(ProtocolError, match="Connection broken") as caught:
                    response.read()
                assert isinstance(caught.value.__cause__, OSError)
                assert conn.is_closed
            finally:
                response.close()

    def test_oversized_read1_from_transport(self) -> None:
        self.start_response_handler(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nbody")
        with HTTPConnectionPool(self.host, self.port, timeout=5) as pool:
            response = pool.urlopen("GET", "/", preload_content=False)
            assert response.read1(2**31) == b"body"
            assert response.read1() == b""

    @pytest.mark.parametrize("partial", [False, True])
    @pytest.mark.parametrize("chunked", [False, True])
    def test_compressed_response_read_boundaries(
        self,
        compressed_payload: tuple[str, bytes, bytes],
        partial: bool,
        chunked: bool,
    ) -> None:
        encoding, encoded, plain = compressed_payload
        headers = f"HTTP/1.1 200 OK\r\nContent-Encoding: {encoding}\r\nConnection: close\r\n".encode()
        if chunked:
            wire = (
                headers
                + b"Transfer-Encoding: chunked\r\n\r\n"
                + f"{len(encoded):x}\r\n".encode()
                + encoded
                + b"\r\n0\r\n\r\n"
            )
        else:
            wire = (
                headers + f"Content-Length: {len(encoded)}\r\n\r\n".encode() + encoded
            )

        def handler(listener: socket.socket) -> None:
            with listener.accept()[0] as sock:
                sock.settimeout(5)
                consume_socket(sock)
                sock.sendall(wire)

        self._start_server(handler)
        with HTTPConnectionPool(self.host, self.port, timeout=5, retries=False) as pool:
            response = pool.urlopen("GET", "/", preload_content=False)
            from urllib3.backend import LowLevelResponse

            assert isinstance(response._fp, LowLevelResponse)
            assert response._fp.from_promise is not None
            assert response.is_from_promise(response._fp.from_promise)
            prefix = response.read(64) if partial else b""
            assert prefix == (plain[:64] if partial else b"")
            assert response.read(0) == b""
            assert response.read1(0) == b""
            assert list(response.stream(0)) == []
            if chunked:
                remainder = b"".join(response.read_chunked(-1))
            else:
                remainder = response.read()
            assert len(prefix + remainder) == len(plain)
            assert prefix + remainder == plain
            response.close()

    @pytest.mark.parametrize("decode_content", [False, True])
    def test_zero_sized_read1_does_not_wait_for_body(
        self, decode_content: bool
    ) -> None:
        send_body = Event()

        def socket_handler(listener: socket.socket) -> None:
            with listener.accept()[0] as sock:
                consume_socket(sock)
                sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n")
                if send_body.wait(10):
                    sock.sendall(b"body")

        self._start_server(socket_handler)
        with HTTPConnectionPool(self.host, self.port, timeout=5) as pool:
            response = pool.urlopen("GET", "/", preload_content=False)
            try:
                # The server sends the body only after the zero-byte read returns.
                assert response.read1(0, decode_content=decode_content) == b""
                assert response.tell() == 0
                assert response.length_remaining == 4
                send_body.set()
                assert response.read(cache_content=True) == b"body"
                assert response.data == b"body"
            finally:
                send_body.set()
                response.close()

    @pytest.mark.parametrize("decode_content", [False, True])
    def test_zero_sized_read_does_not_wait_for_body(self, decode_content: bool) -> None:
        send_body = Event()

        def socket_handler(listener: socket.socket) -> None:
            with listener.accept()[0] as sock:
                consume_socket(sock)
                sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n")
                if send_body.wait(10):
                    sock.sendall(b"body")

        self._start_server(socket_handler)
        with HTTPConnectionPool(self.host, self.port, timeout=5) as pool:
            response = pool.urlopen("GET", "/", preload_content=False)
            try:
                # The server sends the body only after the zero-byte read returns.
                assert response.read(0, decode_content=decode_content) == b""
                assert response.tell() == 0
                assert response.length_remaining == 4
                send_body.set()
                assert response.read(cache_content=True) == b"body"
                assert response.data == b"body"
            finally:
                send_body.set()
                response.close()

    @pytest.mark.parametrize("amt", [-1, -2])
    @pytest.mark.parametrize("chunked", [False, True])
    @pytest.mark.parametrize("compressed", [False, True])
    @pytest.mark.parametrize("decode_content", [False, True])
    def test_read_negative_amt_reads_entire_body(
        self, amt: int, chunked: bool, compressed: bool, decode_content: bool
    ) -> None:
        payload = bytes(range(256)) * 1024
        encoded = payload
        headers = b""
        if compressed:
            compressor = zlib.compressobj(wbits=16 + zlib.MAX_WBITS)
            encoded = compressor.compress(payload) + compressor.flush()
            headers += b"Content-Encoding: gzip\r\n"
        if chunked:
            headers += b"Transfer-Encoding: chunked\r\n"
            wire_body = b"%x\r\n" % len(encoded) + encoded + b"\r\n0\r\n\r\n"
        else:
            headers += b"Content-Length: %d\r\n" % len(encoded)
            wire_body = encoded

        def socket_handler(listener: socket.socket) -> None:
            with listener.accept()[0] as sock:
                consume_socket(sock)
                sock.sendall(b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n" + wire_body)

        self._start_server(socket_handler)
        # Force the body to span multiple backend reads, even when compressed.
        with HTTPConnectionPool(self.host, self.port, blocksize=128) as pool:
            response = pool.request("GET", "/", preload_content=False, timeout=5)
            try:
                assert response.read(amt, decode_content=decode_content) == (
                    payload if decode_content else encoded
                )
                assert response.read() == b""
                assert response.closed
            finally:
                response.close()

    def test_stream_none_unchunked_response_does_not_hang(self) -> None:
        done_event = Event()

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += sock.recv(65536)

            sock.send(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Length: 12\r\n"
                b"Content-type: text/plain\r\n"
                b"\r\n"
                b"hello, world"
            )
            done_event.wait(5)
            sock.close()

        self._start_server(socket_handler)
        with HTTPConnectionPool(self.host, self.port, retries=False) as pool:
            r = pool.request("GET", "/", timeout=LONG_TIMEOUT, preload_content=False)

            # Stream should read to the end.
            assert [b"hello, world"] == list(r.stream(None))

            done_event.set()

    @pytest.mark.parametrize("amt", [-1, 4096, 10000])
    def test_stream_amt_serves_available_data_without_blocking(self, amt: int) -> None:
        # Regression test for https://github.com/jawah/urllib3.future/issues/379
        # SSE-like response: the server sends one 5479 bytes burst, then keeps
        # the connection open (more data may come much later). ``stream(amt)``
        # must serve available data instead of blocking until ``amt`` bytes
        # accumulate. Both traps are exercised here:
        #   amt=10000: burst < amt, the first read used to block on the socket
        #   amt=4096: the buffered 1383 bytes remainder < amt, serving it used
        #             to trigger a blocking socket read
        payload = b"event:put\ndata:" + b"x" * 5462 + b"\n\n"
        assert len(payload) == 5479

        done_event = Event()

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += sock.recv(65536)

            sock.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"\r\n" + (b"%x\r\n" % len(payload)) + payload + b"\r\n"
            )

            done_event.wait(LONG_TIMEOUT * 10)
            sock.sendall(b"0\r\n\r\n")
            sock.close()

        self._start_server(socket_handler)

        with HTTPConnectionPool(self.host, self.port, retries=False) as pool:
            resp = pool.request(
                "GET",
                "/",
                timeout=util.Timeout(connect=LONG_TIMEOUT, read=2),
                preload_content=False,
            )

            stream = resp.stream(amt)

            received = 0

            while received < len(payload):
                # pre-fix: ReadTimeoutError, the client blocked on the socket
                # even though data was available (or already buffered).
                chunk = next(stream)
                assert chunk and (amt < 0 or len(chunk) <= amt)
                received += len(chunk)

            assert received == len(payload)

            done_event.set()

            # consume the end of stream cleanly.
            assert b"".join(stream) == b""

    def test_large_compressed_stream(self) -> None:
        done_event = Event()
        expected_total_length = 296085

        def socket_handler(listener: socket.socket) -> None:
            compress = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
            data = compress.compress(b"x" * expected_total_length)
            data += compress.flush()

            sock = listener.accept()[0]

            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += sock.recv(65536)

            sock.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Length: %d\r\n"
                b"Content-Encoding: gzip\r\n"
                b"\r\n" % (len(data),) + data
            )

            done_event.wait(5)
            sock.close()

        self._start_server(socket_handler)

        with HTTPConnectionPool(self.host, self.port, retries=False) as pool:
            r = pool.request("GET", "/", timeout=LONG_TIMEOUT, preload_content=False)

            # Chunks must all be equal or less than 10240
            # and only the last chunk is allowed to be smaller
            # than 10240.
            total_length = 0
            chunks_smaller_than_10240 = 0
            for chunk in r.stream(10240, decode_content=True):
                assert 0 < len(chunk) <= 10240
                if len(chunk) < 10240:
                    chunks_smaller_than_10240 += 1
                else:
                    assert chunks_smaller_than_10240 == 0
                total_length += len(chunk)

            assert chunks_smaller_than_10240 == 1
            assert expected_total_length == total_length

            done_event.set()


class TestBadContentLength(SocketDummyServerTestCase):
    def test_enforce_content_length_get(self) -> None:
        done_event = Event()

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += sock.recv(65536)

            sock.send(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Length: 22\r\n"
                b"Content-type: text/plain\r\n"
                b"\r\n"
                b"hello, world"
            )
            done_event.wait(LONG_TIMEOUT)
            sock.close()

        self._start_server(socket_handler)
        with HTTPConnectionPool(self.host, self.port, maxsize=1) as conn:
            # Test stream read when content length less than headers claim
            get_response = conn.request(
                "GET", url="/", preload_content=False, enforce_content_length=True
            )
            data = get_response.stream(100)
            # the available 12 bytes are yielded as they arrive; the
            # content-length violation surfaces when reaching the premature
            # end of stream.
            assert next(data) == b"hello, world"
            done_event.set()
            with pytest.raises(
                IncompleteRead, match=r"12 bytes read\, 10 more expected"
            ):
                next(data)

    def test_enforce_content_length_no_body(self) -> None:
        done_event = Event()

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += sock.recv(65536)

            sock.send(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Length: 22\r\n"
                b"Content-type: text/plain\r\n"
                b"\r\n"
            )
            done_event.wait(1)
            sock.close()

        self._start_server(socket_handler)
        with HTTPConnectionPool(self.host, self.port, maxsize=1) as conn:
            # Test stream on 0 length body
            head_response = conn.request(
                "HEAD", url="/", preload_content=False, enforce_content_length=True
            )
            data = [chunk for chunk in head_response.stream(1)]
            assert len(data) == 0

            done_event.set()


class TestRetryPoolSizeDrainFail(SocketDummyServerTestCase):
    def test_pool_size_retry_drain_fail(self) -> None:
        def socket_handler(listener: socket.socket) -> None:
            for _ in range(2):
                sock = listener.accept()[0]
                while not sock.recv(65536).endswith(b"\r\n\r\n"):
                    pass

                # send a response with an invalid content length -- this causes
                # a ProtocolError to raise when trying to drain the connection
                sock.send(
                    b"HTTP/1.1 404 NOT FOUND\r\n"
                    b"Content-Length: 1000\r\n"
                    b"Content-Type: text/plain\r\n"
                    b"\r\n"
                )
                sock.close()

        self._start_server(socket_handler)
        retries = Retry(total=1, raise_on_status=False, status_forcelist=[404])
        with HTTPConnectionPool(
            self.host, self.port, maxsize=10, retries=retries, block=True
        ) as pool:
            pool.urlopen("GET", "/not_found", preload_content=False)
            assert pool.num_connections == 1


class TestBrokenPipe(SocketDummyServerTestCase):
    @notWindows()
    @notMacOS()
    def test_ignore_broken_pipe_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # On Windows an aborted connection raises an error on
        # attempts to read data out of a socket that's been closed.
        sock_shut = Event()
        orig_connect = HTTPConnection.connect
        # a buffer that will cause two sendall calls
        buf = "a" * 1024 * 1024 * 4

        def connect_and_wait(*args: typing.Any, **kw: typing.Any) -> None:
            ret = orig_connect(*args, **kw)
            assert sock_shut.wait(5)
            return ret

        def socket_handler(listener: socket.socket) -> None:
            for i in range(2):
                sock = listener.accept()[0]
                sock.send(
                    b"HTTP/1.1 404 Not Found\r\n"
                    b"Connection: close\r\n"
                    b"Content-Length: 10\r\n"
                    b"\r\n"
                    b"xxxxxxxxxx"
                )
                sock.shutdown(socket.SHUT_RDWR)
                sock_shut.set()
                sock.close()

        monkeypatch.setattr(HTTPConnection, "connect", connect_and_wait)
        self._start_server(socket_handler)
        with HTTPConnectionPool(self.host, self.port) as pool:
            r = pool.request("POST", "/", body=buf)
            assert r.status == 404
            assert r.headers["content-length"] == "10"
            assert r.data == b"xxxxxxxxxx"

            r = pool.request("POST", "/admin", chunked=True, body=buf)
            assert r.status == 404
            assert r.headers["content-length"] == "10"
            assert r.data == b"xxxxxxxxxx"


class TestMultipartResponse(SocketDummyServerTestCase):
    def test_multipart_assert_header_parsing_no_defects(self) -> None:
        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            while not sock.recv(65536).endswith(b"\r\n\r\n"):
                pass

            sock.sendall(
                b"HTTP/1.1 404 Not Found\r\n"
                b"Server: example.com\r\n"
                b"Content-Type: multipart/mixed; boundary=36eeb8c4e26d842a\r\n"
                b"Content-Length: 73\r\n"
                b"\r\n"
                b"--36eeb8c4e26d842a\r\n"
                b"Content-Type: text/plain\r\n"
                b"\r\n"
                b"1\r\n"
                b"--36eeb8c4e26d842a--\r\n",
            )
            sock.close()

        self._start_server(socket_handler)
        from urllib3.connectionpool import log

        with mock.patch.object(log, "warning") as log_warning:
            with HTTPConnectionPool(self.host, self.port, timeout=LONG_TIMEOUT) as pool:
                resp = pool.urlopen("GET", "/")
                assert resp.status == 404
                assert (
                    resp.headers["content-type"]
                    == "multipart/mixed; boundary=36eeb8c4e26d842a"
                )
                assert len(resp.data) == 73
                log_warning.assert_not_called()


class TestContentFraming(SocketDummyServerTestCase):
    @pytest.mark.parametrize("content_length", [None, 0])
    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
    def test_content_length_0_by_default(
        self, method: str, content_length: int | None
    ) -> None:
        buffer = bytearray()

        def socket_handler(listener: socket.socket) -> None:
            nonlocal buffer
            sock = listener.accept()[0]
            while not buffer.endswith(b"\r\n\r\n"):
                buffer += sock.recv(65536)
            sock.sendall(
                b"HTTP/1.1 200 OK\r\nServer: example.com\r\nContent-Length: 0\r\n\r\n"
            )
            sock.close()

        self._start_server(socket_handler)

        headers = {}
        if content_length is not None:
            headers["Content-Length"] = str(content_length)

        with HTTPConnectionPool(self.host, self.port, timeout=3) as pool:
            resp = pool.request(method, "/")
            assert resp.status == 200

        sent_bytes = bytes(buffer)
        assert b"Accept-Encoding: identity\r\n" in sent_bytes
        assert b"Content-Length: 0\r\n" in sent_bytes
        assert b"transfer-encoding" not in sent_bytes.lower()

    @pytest.mark.parametrize("chunked", [True, False])
    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
    @pytest.mark.parametrize("body_type", ["file", "generator", "bytes"])
    def test_chunked_specified(
        self, method: str, chunked: bool, body_type: str
    ) -> None:
        buffer = bytearray()

        def socket_handler(listener: socket.socket) -> None:
            nonlocal buffer
            with listener.accept()[0] as sock:
                sock.settimeout(LONG_TIMEOUT)
                while not buffer.endswith(b"\r\n0\r\n\r\n"):
                    data = sock.recv(65536)
                    assert data, (
                        "Client closed before sending the complete chunked body"
                    )
                    buffer += data

                sock.sendall(
                    b"HTTP/1.1 200 OK\r\nServer: example.com\r\nContent-Length: 0\r\n\r\n"
                )

        body: typing.Any
        if body_type == "generator":

            def body_generator() -> typing.Generator[bytes, None, None]:
                yield b"x" * 10

            body = body_generator()
        elif body_type == "file":
            body = io.BytesIO(b"x" * 10)
            body.seek(0, 0)
        else:
            if chunked is False:
                pytest.skip("urllib3 uses Content-Length in this case")
            body = b"x" * 10

        self._start_server(socket_handler)

        with HTTPConnectionPool(
            self.host, self.port, timeout=LONG_TIMEOUT, retries=False
        ) as pool:
            resp = pool.request(method, "/", chunked=chunked, body=body)
            assert resp.status == 200

        sent_bytes = bytes(buffer)
        assert sent_bytes.count(b":") == 5
        assert b"Host: localhost:" in sent_bytes
        assert b"Accept-Encoding: identity\r\n" in sent_bytes
        assert b"Transfer-Encoding: chunked\r\n" in sent_bytes
        assert b"User-Agent: urllib3.future/" in sent_bytes
        assert b"content-length" not in sent_bytes.lower()
        assert b"\r\n\r\na\r\nxxxxxxxxxx\r\n0\r\n\r\n" in sent_bytes

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
    @pytest.mark.parametrize(
        "body_type", ["file", "generator", "bytes", "bytearray", "file_text"]
    )
    def test_chunked_not_specified(self, method: str, body_type: str) -> None:
        buffer = bytearray()

        def socket_handler(listener: socket.socket) -> None:
            nonlocal buffer
            sock = listener.accept()[0]
            sock.settimeout(0)

            start = time.time()
            while time.time() - start < (LONG_TIMEOUT / 2):
                try:
                    buffer += sock.recv(65536)
                except OSError:
                    continue

            sock.sendall(
                b"HTTP/1.1 200 OK\r\nServer: example.com\r\nContent-Length: 0\r\n\r\n"
            )
            sock.close()

        self._start_server(socket_handler)

        body: typing.Any
        if body_type == "generator":

            def body_generator() -> typing.Generator[bytes, None, None]:
                yield b"x" * 10

            body = body_generator()
            should_be_chunked = True

        elif body_type == "file":
            body = io.BytesIO(b"x" * 10)
            body.seek(0, 0)
            should_be_chunked = True

        elif body_type == "file_text":
            body = io.StringIO("x" * 10)
            body.seek(0, 0)
            should_be_chunked = True

        elif body_type == "bytearray":
            body = bytearray(b"x" * 10)
            should_be_chunked = False

        else:
            body = b"x" * 10
            should_be_chunked = False

        with HTTPConnectionPool(
            self.host, self.port, timeout=LONG_TIMEOUT, retries=False
        ) as pool:
            resp = pool.request(method, "/", body=body)
            assert resp.status == 200

        sent_bytes = bytes(buffer)
        if body_type == "file_text":
            assert sent_bytes.count(b":") == 6
        else:
            assert sent_bytes.count(b":") == 5
        assert b"Host: localhost:" in sent_bytes
        assert b"Accept-Encoding: identity\r\n" in sent_bytes
        assert b"User-Agent: urllib3.future/" in sent_bytes

        if should_be_chunked:
            assert b"content-length" not in sent_bytes.lower()
            assert b"Transfer-Encoding: chunked\r\n" in sent_bytes
            assert b"\r\n\r\na\r\nxxxxxxxxxx\r\n0\r\n\r\n" in sent_bytes

        else:
            assert b"Content-Length: 10\r\n" in sent_bytes
            assert b"transfer-encoding" not in sent_bytes.lower()
            assert sent_bytes.endswith(b"\r\n\r\nxxxxxxxxxx")

    def test_overrule_str_content_type(self) -> None:
        buffer = bytearray()

        def socket_handler(listener: socket.socket) -> None:
            nonlocal buffer
            sock = listener.accept()[0]
            sock.settimeout(0)

            start = time.time()
            while time.time() - start < (LONG_TIMEOUT / 2):
                try:
                    buffer += sock.recv(65536)
                except OSError:
                    continue

            sock.sendall(
                b"HTTP/1.1 200 OK\r\nServer: example.com\r\nContent-Length: 0\r\n\r\n"
            )
            sock.close()

        self._start_server(socket_handler)

        with HTTPConnectionPool(
            self.host, self.port, timeout=LONG_TIMEOUT, retries=False
        ) as pool:
            resp = pool.request("POST", "/", body="fooéàè" * 3)
            assert resp.status == 200

        sent_bytes = bytes(buffer)

        assert b"Content-Type: text/plain; charset=utf-8\r\n" in sent_bytes

    def test_partial_overrule_str_content_type(self) -> None:
        buffer = bytearray()

        def socket_handler(listener: socket.socket) -> None:
            nonlocal buffer
            sock = listener.accept()[0]
            sock.settimeout(0)

            start = time.time()
            while time.time() - start < (LONG_TIMEOUT / 2):
                try:
                    buffer += sock.recv(65536)
                except OSError:
                    continue

            sock.sendall(
                b"HTTP/1.1 200 OK\r\nServer: example.com\r\nContent-Length: 0\r\n\r\n"
            )
            sock.close()

        self._start_server(socket_handler)

        with HTTPConnectionPool(
            self.host, self.port, timeout=LONG_TIMEOUT, retries=False
        ) as pool:
            resp = pool.request(
                "POST", "/", body="{}", headers={"Content-Type": "application/json"}
            )
            assert resp.status == 200

        sent_bytes = bytes(buffer)

        assert b"Content-Type: application/json\r\n" in sent_bytes

    def test_partial_overrule_bytes_content_type(self) -> None:
        buffer = bytearray()

        def socket_handler(listener: socket.socket) -> None:
            nonlocal buffer
            sock = listener.accept()[0]
            sock.settimeout(0)

            start = time.time()
            while time.time() - start < (LONG_TIMEOUT / 2):
                try:
                    buffer += sock.recv(65536)
                except OSError:
                    continue

            sock.sendall(
                b"HTTP/1.1 200 OK\r\nServer: example.com\r\nContent-Length: 0\r\n\r\n"
            )
            sock.close()

        self._start_server(socket_handler)

        with HTTPConnectionPool(
            self.host, self.port, timeout=LONG_TIMEOUT, retries=False
        ) as pool:
            resp = pool.request(
                "POST",
                "/",
                body="{}",
                headers={"Content-Type": b"application/json"},  # type: ignore[dict-item]
            )
            assert resp.status == 200

        sent_bytes = bytes(buffer)

        assert b"Content-Type: application/json\r\n" in sent_bytes

    def test_no_overrule_str_content_type(self) -> None:
        buffer = bytearray()

        def socket_handler(listener: socket.socket) -> None:
            nonlocal buffer
            with listener.accept()[0] as sock:
                sock.settimeout(5)
                # Wait for the complete request, including its two-byte body.
                while not buffer.endswith(b"\r\n\r\n{}"):
                    data = sock.recv(65536)
                    assert data, "Client closed before sending the complete request"
                    buffer += data

                sock.sendall(
                    b"HTTP/1.1 200 OK\r\nServer: example.com\r\nContent-Length: 0\r\n\r\n"
                )

        self._start_server(socket_handler)

        with warnings.catch_warnings(record=True) as w:
            with HTTPConnectionPool(
                self.host, self.port, timeout=LONG_TIMEOUT, retries=False
            ) as pool:
                resp = pool.request(
                    "POST",
                    "/",
                    body="{}",
                    headers={"Content-Type": "application/json; charset=ascii"},
                )
                assert resp.status == 200

        assert any(
            isinstance(x.message, Warning)
            and "A conflicting charset has been set in" in x.message.args[0]
            for x in w
        )

        sent_bytes = bytes(buffer)

        assert b"Content-Type: application/json; charset=ascii\r\n" in sent_bytes

    @pytest.mark.parametrize(
        "header_transform",
        [str.lower, str.title, str.upper],
    )
    @pytest.mark.parametrize(
        ["header", "header_value", "expected"],
        [
            ("content-length", "8", b": 8\r\n\r\nxxxxxxxx"),
            (
                "transfer-encoding",
                "chunked",
                b": chunked\r\n\r\n8\r\nxxxxxxxx\r\n0\r\n\r\n",
            ),
        ],
    )
    def test_framing_set_via_headers(
        self,
        header_transform: typing.Callable[[str], str],
        header: str,
        header_value: str,
        expected: bytes,
    ) -> None:
        buffer = bytearray()

        def socket_handler(listener: socket.socket) -> None:
            nonlocal buffer
            sock = listener.accept()[0]
            sock.settimeout(0)

            start = time.time()
            while time.time() - start < (LONG_TIMEOUT / 2):
                try:
                    buffer += sock.recv(65536)
                except OSError:
                    continue

            sock.sendall(
                b"HTTP/1.1 200 OK\r\nServer: example.com\r\nContent-Length: 0\r\n\r\n"
            )
            sock.close()

        self._start_server(socket_handler)

        with HTTPConnectionPool(
            self.host, self.port, timeout=LONG_TIMEOUT, retries=False
        ) as pool:
            resp = pool.request(
                "POST",
                "/",
                body=b"xxxxxxxx",
                headers={header_transform(header): header_value},
            )
            assert resp.status == 200

            sent_bytes = bytes(buffer)
            assert sent_bytes.endswith(expected)


class TestRemoteClosedWithoutResponse(SocketDummyServerTestCase):
    """Covers the ``ConnectionTerminated`` event branch in ``__exchange_until``
    where the server closes the connection after the request was sent but
    before any HTTP/1.1 response headers are emitted. See ``"Remote end closed connection without response"``
    raise (and async mirror).
    """

    def test_http2_disposed_stream_ignores_late_data(self) -> None:
        from jh2.events import StreamReset
        from jh2.hyperframe.frame import DataFrame

        reset_received = Event()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(DEFAULT_CERTS["certfile"], DEFAULT_CERTS["keyfile"])
        context.set_alpn_protocols(["h2"])

        def handler(listener: socket.socket) -> None:
            with listener.accept()[0] as raw:
                raw.settimeout(5)
                with context.wrap_socket(raw, server_side=True) as sock:
                    h2 = H2Connection(config=H2Configuration(client_side=False))
                    h2.initiate_connection()
                    sock.sendall(h2.data_to_send())
                    streams: list[int] = []
                    while len(streams) < 2:
                        data = sock.recv(65536)
                        assert data
                        for event in h2.receive_data(data):
                            if isinstance(event, RequestReceived):
                                streams.append(event.stream_id)
                                h2.send_headers(
                                    event.stream_id,
                                    [
                                        (b":status", b"200"),
                                        (
                                            b"content-length",
                                            b"3" if len(streams) == 1 else b"2",
                                        ),
                                    ],
                                )
                                if len(streams) == 1:
                                    h2.send_data(event.stream_id, b"a")
                        sock.sendall(h2.data_to_send())
                    while not reset_received.is_set():
                        data = sock.recv(65536)
                        assert data
                        for event in h2.receive_data(data):
                            if (
                                isinstance(event, StreamReset)
                                and event.stream_id == streams[0]
                            ):
                                assert event.error_code == 8
                                reset_received.set()
                        sock.sendall(h2.data_to_send())
                    # Simulate DATA already in flight when the reset crossed it.
                    late = DataFrame(streams[0])
                    late.data = b"bc"
                    late.flags.add("END_STREAM")
                    h2.send_data(streams[1], b"ok", end_stream=True)
                    sock.sendall(late.serialize() + h2.data_to_send())
                    while len(streams) < 3:
                        data = sock.recv(65536)
                        assert data
                        for event in h2.receive_data(data):
                            if isinstance(event, RequestReceived):
                                streams.append(event.stream_id)
                                h2.send_headers(
                                    event.stream_id,
                                    [
                                        (b":status", b"200"),
                                        (b"content-length", b"2"),
                                    ],
                                )
                                h2.send_data(event.stream_id, b"ok", end_stream=True)
                        sock.sendall(h2.data_to_send())

        self._start_server(handler)
        with HTTPSConnectionPool(
            self.host,
            self.port,
            ca_certs=DEFAULT_CA,
            timeout=5,
            retries=False,
            disabled_svn={HttpVersion.h11, HttpVersion.h3},
            background_watch_delay=None,
        ) as pool:
            first = pool.request("GET", "/first", preload_content=False)
            conn = first.connection
            assert conn is not None and conn._protocol is not None
            protocol = conn._protocol
            assert first.read(1) == b"a"
            second = pool.request("GET", "/second", preload_content=False)
            assert second.connection is conn
            first.close()
            assert reset_received.wait(5)
            assert second.read() == b"ok"
            assert protocol.is_idle()
            assert not protocol.has_pending_event(stream_id=1)
            third = pool.request("GET", "/third", preload_content=False)
            assert third.connection is conn
            assert third.read() == b"ok"

    @pytest.mark.parametrize(
        "error_code,error_name",
        [
            (0xB, "ENHANCE_YOUR_CALM"),
            (0x1, "PROTOCOL_ERROR"),
            (0xFFFF, "UNKNOWN_ERROR"),
        ],
    )
    @pytest.mark.parametrize("multiplexed", [False, True])
    def test_http2_goaway_error_message(
        self, error_code: int, error_name: str, multiplexed: bool
    ) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(DEFAULT_CERTS["certfile"], DEFAULT_CERTS["keyfile"])
        context.set_alpn_protocols(["h2"])

        def handler(listener: socket.socket) -> None:
            listener.settimeout(5)
            with listener.accept()[0] as raw:
                raw.settimeout(5)
                with context.wrap_socket(raw, server_side=True) as sock:
                    h2 = H2Connection(config=H2Configuration(client_side=False))
                    h2.initiate_connection()
                    sock.sendall(h2.data_to_send())
                    received = False
                    while not received:
                        data = sock.recv(65536)
                        assert data
                        for event in h2.receive_data(data):
                            if isinstance(event, RequestReceived):
                                h2.close_connection(
                                    error_code=error_code,
                                    last_stream_id=event.stream_id,
                                )
                                received = True
                        sock.sendall(h2.data_to_send())
                    # Keep TCP open until the client processes the actual GOAWAY.
                    try:
                        while sock.recv(65536):
                            pass
                    except (ssl.SSLError, ConnectionResetError):
                        pass

        self._start_server(handler)
        with HTTPSConnectionPool(
            self.host,
            self.port,
            ca_certs=DEFAULT_CA,
            timeout=5,
            disabled_svn={HttpVersion.h11, HttpVersion.h3},
            retries=False,
        ) as pool:
            with pytest.raises(ProtocolError) as caught:
                if multiplexed:
                    promise = pool.urlopen("GET", "/", multiplexed=True)
                    assert isinstance(promise, ResponsePromise)
                    pool.get_response(promise=promise)
                else:
                    pool.urlopen("GET", "/")
            message = str(caught.value)
            assert "HTTP/2" in message
            assert "GOAWAY" in message
            assert error_name in message
            assert hex(error_code) in message

    @pytest.mark.parametrize(
        "error_code,frame_type,reason,error_name,error_type",
        [
            (0x107, None, "too many requests", "H3_EXCESSIVE_LOAD", ProtocolError),
            (
                0x200,
                None,
                "invalid QPACK data",
                "QPACK_DECOMPRESSION_FAILED",
                ProtocolError,
            ),
            # The same number belongs to different namespaces on the wire.
            (0x10A, None, "", "H3_MISSING_SETTINGS", ProtocolError),
            (
                0x10A,
                0x6,
                "unexpected handshake message",
                "TLS_UNEXPECTED_MESSAGE",
                SSLError,
            ),
            (0x12A, None, "custom application error", "UNKNOWN_ERROR", ProtocolError),
            (
                0x12A,
                0x6,
                "peer rejected client certificate",
                "TLS_BAD_CERTIFICATE",
                SSLError,
            ),
            (0x128, 0x6, "no compatible cipher", "TLS_HANDSHAKE_FAILURE", SSLError),
            (0x3, 0x8, "flow control exceeded", "FLOW_CONTROL_ERROR", ProtocolError),
            (0xFFFF, None, "custom application error", "UNKNOWN_ERROR", ProtocolError),
            (0x1FF, 0x6, "", "UNKNOWN_ERROR", SSLError),
            # TLS classification must precede the legacy code-400/header heuristic.
            (0x190, 0x6, "invalid header", "UNKNOWN_ERROR", SSLError),
        ],
    )
    @pytest.mark.parametrize("multiplexed", [False, True])
    def test_http3_close_error_message(
        self,
        http3_error_server: ServerConfig,
        error_code: int,
        frame_type: int | None,
        reason: str,
        error_name: str,
        error_type: type[Exception],
        multiplexed: bool,
    ) -> None:
        with HTTPSConnectionPool(
            http3_error_server.host,
            http3_error_server.port,
            ca_certs=http3_error_server.ca_certs,
            timeout=5,
            disabled_svn={HttpVersion.h11, HttpVersion.h2},
            retries=False,
        ) as pool:
            with pytest.raises(error_type) as caught:
                if multiplexed:
                    promise = pool.urlopen("GET", "/", multiplexed=True)
                    assert isinstance(promise, ResponsePromise)
                    pool.get_response(promise=promise)
                else:
                    pool.urlopen("GET", "/")
            message = str(caught.value)
            assert "HTTP/3" in message
            assert error_name in message
            assert hex(error_code) in message
            if reason:
                assert reason in message

    @pytest.mark.parametrize(
        "error_code, multiplexed",
        [(0xD, False), (0x8, False), (0x8, True)],
    )
    def test_http2_peer_reset(
        self,
        error_code: int,
        multiplexed: bool,
    ) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(DEFAULT_CERTS["certfile"], DEFAULT_CERTS["keyfile"])
        context.set_alpn_protocols(["h2"])

        def handler(listener: socket.socket) -> None:
            with listener.accept()[0] as raw:
                raw.settimeout(5)
                with context.wrap_socket(raw, server_side=True) as sock:
                    h2 = H2Connection(config=H2Configuration(client_side=False))
                    h2.initiate_connection()
                    sock.sendall(h2.data_to_send())
                    received = False
                    while not received:
                        data = sock.recv(65536)
                        assert data
                        for event in h2.receive_data(data):
                            if isinstance(event, RequestReceived):
                                h2.reset_stream(event.stream_id, error_code=error_code)
                                received = True
                        sock.sendall(h2.data_to_send())

        self._start_server(handler)
        with HTTPSConnectionPool(
            self.host,
            self.port,
            ca_certs=DEFAULT_CA,
            timeout=5,
            disabled_svn={HttpVersion.h11, HttpVersion.h3},
            retries=False,
        ) as pool:
            with pytest.raises(ProtocolError, match="reset by remote peer"):
                if multiplexed:
                    result = pool.urlopen("GET", "/", multiplexed=True)
                    assert isinstance(result, ResponsePromise)
                    pool.get_response()
                else:
                    pool.urlopen("GET", "/")

    @pytest.mark.parametrize("through_manager", [False, True])
    @pytest.mark.parametrize("by_promise", [False, True])
    def test_retired_pool_connection_error_releases_registry(
        self, through_manager: bool, by_promise: bool
    ) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(DEFAULT_CERTS["certfile"], DEFAULT_CERTS["keyfile"])
        context.set_alpn_protocols(["h2"])
        terminate = Event()

        def handler(listener: socket.socket) -> None:
            listener.settimeout(5)
            with listener.accept()[0] as raw:
                raw.settimeout(5)
                with context.wrap_socket(raw, server_side=True) as sock:
                    h2 = H2Connection(config=H2Configuration(client_side=False))
                    h2.initiate_connection()
                    sock.sendall(h2.data_to_send())
                    while True:
                        data = sock.recv(65536)
                        assert data
                        for event in h2.receive_data(data):
                            if isinstance(event, RequestReceived):
                                assert terminate.wait(5)
                                h2.close_connection(
                                    error_code=0x2, last_stream_id=event.stream_id
                                )
                                sock.sendall(h2.data_to_send())
                                return
                        sock.sendall(h2.data_to_send())

        self._start_server(handler)
        try:
            with PoolManager(
                ca_certs=DEFAULT_CA,
                disabled_svn={HttpVersion.h11, HttpVersion.h3},
                timeout=5,
                retries=False,
            ) as manager:
                promise = manager.urlopen(
                    "GET", f"https://{self.host}:{self.port}/", multiplexed=True
                )
                assert isinstance(promise, ResponsePromise)
                old_pool = next(iter(manager.pools._registry.values()))
                police = old_pool.pool
                assert police is not None
                sock = promise._conn.sock
                assert sock is not None
                manager.clear()
                terminate.set()
                reader = manager if through_manager else old_pool
                with pytest.raises(ProtocolError, match="GOAWAY"):
                    reader.get_response(promise=promise if by_promise else None)
                assert sock.fileno() == -1
                assert police.rsize() == 0
                assert manager.pools.rsize() == 0
        finally:
            terminate.set()

    def test_http2_rejects_upload_before_body_is_consumed(self) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(DEFAULT_CERTS["certfile"], DEFAULT_CERTS["keyfile"])
        context.set_alpn_protocols(["h2"])
        finished = Event()
        produced = []

        def body() -> typing.Iterator[bytes]:
            for i in range(32):
                produced.append(i)
                yield b"x" * 16384

        def handler(listener: socket.socket) -> None:
            try:
                with listener.accept()[0] as raw:
                    raw.settimeout(5)
                    with context.wrap_socket(raw, server_side=True) as sock:
                        h2 = H2Connection(config=H2Configuration(client_side=False))
                        h2.initiate_connection()
                        sock.sendall(h2.data_to_send())
                        received = False
                        while not received:
                            data = sock.recv(65536)
                            assert data
                            for event in h2.receive_data(data):
                                if isinstance(event, RequestReceived):
                                    h2.send_headers(
                                        event.stream_id,
                                        [(":status", "413"), ("content-length", "0")],
                                        end_stream=True,
                                    )
                                    received = True
                            sock.sendall(h2.data_to_send())
                        # Drain without granting further HTTP/2 flow-control credit.
                        # The client must notice the response and stop its upload.
                        try:
                            while sock.recv(65536):
                                pass
                        except (ssl.SSLError, ConnectionResetError):
                            pass
            finally:
                finished.set()

        self._start_server(handler)
        with HTTPSConnectionPool(
            self.host,
            self.port,
            ca_certs=DEFAULT_CA,
            timeout=5,
            disabled_svn={HttpVersion.h11, HttpVersion.h3},
            retries=False,
        ) as pool:
            response = pool.request("POST", "/", body=body())
            assert response.status == 413 and response.version == 20
            assert (response.data) == b""
            assert 0 < len(produced) < 32
        assert finished.wait(5)

    def test_server_closes_socket_before_status_line(self) -> None:
        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            # Drain the request, then close immediately without sending anything.
            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += sock.recv(65536)
            sock.close()

        self._start_server(socket_handler)
        with HTTPConnectionPool(
            self.host, self.port, timeout=LONG_TIMEOUT, retries=False
        ) as pool:
            with pytest.raises(
                ProtocolError, match="Remote end closed connection without response"
            ):
                pool.request("GET", "/")

    def test_remote_close_cause_does_not_retry_post(self) -> None:
        body = b"already processed"
        received = []

        def socket_handler(listener: socket.socket) -> None:
            listener.settimeout(5)
            with listener.accept()[0] as sock:
                sock.settimeout(5)
                with sock.makefile("rb") as request:
                    assert request.readline().startswith(b"POST / ")
                    while request.readline() not in (b"\r\n", b""):
                        pass
                    received.append(request.read(len(body)))
                # The request was delivered; close without acknowledging it.

        self._start_server(socket_handler)
        with HTTPConnectionPool(self.host, self.port, timeout=5, retries=1) as pool:
            with pytest.raises(
                ProtocolError, match="Remote end closed connection without response"
            ) as caught:
                pool.request("POST", "/", body=body)

            assert isinstance(caught.value.__cause__, ConnectionResetError)
            assert str(caught.value.__cause__) == str(caught.value)
            assert pool.num_requests == pool.num_connections == 1
        assert received == [body]


class TestInvalidHTTPResponse(SocketDummyServerTestCase):
    """Covers the h11 ``RemoteProtocolError`` -> ``InvalidHeader`` translation
    in ``__exchange_until`` (lines ~1150-1155 sync, async mirror). h11 raises
    400 with a message containing ``header`` when it sees a malformed response
    header on the wire.
    """

    def test_garbage_header_separator_raises_invalid_header(self) -> None:
        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += sock.recv(65536)
            # Status line is OK; the header line is malformed (no colon).
            sock.sendall(
                b"HTTP/1.1 200 OK\r\nNoColonHeaderLine\r\nContent-Length: 0\r\n\r\n"
            )
            sock.close()

        self._start_server(socket_handler)
        with HTTPConnectionPool(
            self.host, self.port, timeout=LONG_TIMEOUT, retries=False
        ) as pool:
            with pytest.raises((InvalidHeader, ProtocolError)):
                pool.request("GET", "/")


class TestPartialBodyClose(SocketDummyServerTestCase):
    """Covers the alternative ``IncompleteRead`` message-string parsing path
    when h11 surfaces a ``RemoteProtocolError`` with
    ``peer closed connection without sending complete message body``. This is
    distinct from ``InvalidBodyLengthError`` which already has structured
    attributes and is covered by ``TestBadContentLength``.
    """

    def test_server_closes_after_partial_body(self) -> None:
        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            buf = b""
            while not buf.endswith(b"\r\n\r\n"):
                buf += sock.recv(65536)
            # Announce 50 bytes, send 10, then drop the connection.
            sock.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Length: 50\r\n"
                b"Content-Type: text/plain\r\n"
                b"\r\n"
                b"0123456789"
            )
            sock.close()

        self._start_server(socket_handler)
        with HTTPConnectionPool(
            self.host, self.port, timeout=LONG_TIMEOUT, retries=False
        ) as pool:
            resp = pool.request("GET", "/", preload_content=False, retries=False)
            with pytest.raises((IncompleteRead, ProtocolError)):
                resp.read()


class TestSyncRejectsAsyncIterableBody(SocketDummyServerTestCase):
    """Covers the ``hasattr(chunks, "__aiter__")`` rejection branch in
    ``src/urllib3/connection.py`` request body sending path. A synchronous
    connection cannot drive an ``async`` iterator, so the library raises
    ``RuntimeError`` rather than silently mis-sending the body.
    """

    def test_async_iter_body_raises_runtime_error(self) -> None:
        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]
            # Drain whatever headers arrive, then echo back a minimal 200 so
            # the request lifecycle can complete cleanly if it ever gets that
            # far (it should not -- RuntimeError fires before any body byte).
            buf = b""
            try:
                while b"\r\n\r\n" not in buf:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            finally:
                sock.close()

        async def _async_body() -> typing.AsyncIterator[bytes]:
            yield b"hello"
            yield b"world"

        self._start_server(socket_handler)
        with HTTPConnectionPool(
            self.host, self.port, timeout=LONG_TIMEOUT, retries=False
        ) as pool:
            with pytest.raises(
                RuntimeError,
                match="Unable to send an async iterable through a synchronous connection",
            ):
                pool.urlopen(  # type: ignore[call-overload]
                    "POST",
                    "/",
                    body=_async_body(),
                    chunked=True,
                )


class TestConnectionUtilities(SocketDummyServerTestCase):
    @pytest.mark.parametrize("compressed", [False, True])
    @pytest.mark.parametrize("decode_content", [False, True])
    def test_read1_size_preserves_remainder(
        self, compressed: bool, decode_content: bool
    ) -> None:
        payload = b"response body" * 100
        body = zlib.compress(payload) if compressed else payload
        encoding = b"Content-Encoding: deflate\r\n" if compressed else b""
        self.start_response_handler(
            b"HTTP/1.1 200 OK\r\n"
            + encoding
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        with HTTPConnectionPool(self.host, self.port, timeout=5) as pool:
            response = pool.urlopen("GET", "/", preload_content=False)
            try:
                received = b""
                for size in (1, 7, 2, 16):
                    chunk = response.read1(size, decode_content=decode_content)
                    assert len(chunk) <= size
                    received += chunk
                received += response.read(decode_content=decode_content)
                assert received == (payload if decode_content else body)
            finally:
                response.close()

    @pytest.mark.parametrize("incomplete_utf8", [False, True])
    def test_sse_utf8_at_eof(self, incomplete_utf8: bool) -> None:
        body = "data: first €\n\n".encode()
        if incomplete_utf8:
            body += b"data: incomplete \xe2\x82"
        self.start_response_handler(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        with PoolManager(timeout=5) as manager:
            response = manager.urlopen("GET", f"psse://{self.host}:{self.port}/")
            assert response.extension is not None
            event = response.extension.next_payload()
            assert isinstance(event, ServerSentEvent)
            assert event.data == "first €"
            if incomplete_utf8:
                with pytest.raises(UnicodeDecodeError):
                    response.extension.next_payload()
            else:
                assert response.extension.next_payload() is None
                assert response.extension.closed
        assert manager.pools.rsize() == 0

    def test_early_hints_without_callback(self) -> None:
        self.start_response_handler(
            b"HTTP/1.1 103 Early Hints\r\nLink: </asset>; rel=preload\r\n\r\n"
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
        )
        with HTTPConnectionPool(self.host, self.port, timeout=5) as pool:
            response = pool.urlopen("GET", "/")
            assert response.status == 200
            assert response.data == b"ok"

    def test_legacy_create_connection_and_udp_socket_options(self) -> None:
        def handler(listener: socket.socket) -> None:
            with listener.accept()[0] as sock:
                sock.settimeout(5)
                assert sock.recv(4) == b"ping"
                sock.sendall(b"pong")

        self._start_server(handler)
        with util.connection.create_connection(
            (self.host, self.port), timeout=5
        ) as sock:
            sock.sendall(b"ping")
            assert sock.recv(4) == b"pong"
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as datagram:
            util.connection._set_socket_options(
                datagram, [(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1, "udp")]
            )
            assert datagram.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)

    def test_sse_leading_empty_line(self) -> None:
        # SSE accepts a lone CR as a line separator, including empty field lines.
        body = b"\rdata: first\n\n"
        self.start_response_handler(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        assert ServerSideEventExtensionFromHTTP.implementation() == "native"
        assert ServerSentEvent().data == ""
        with PoolManager(timeout=5) as manager:
            response = manager.urlopen("GET", f"psse://{self.host}:{self.port}/")
            assert response.extension is not None
            event = response.extension.next_payload()
            assert isinstance(event, ServerSentEvent)
            assert event.data == "first"
            response.extension.close()
            response.close()


class TestExtensionStartup(SocketDummyServerTestCase):
    def test_extension_rejects_plain_response(self) -> None:
        from io import BytesIO

        response = HTTPResponse(body=BytesIO(b"body"), preload_content=False)
        with pytest.raises(RuntimeError, match="without direct I/O access"):
            RawExtensionFromHTTP().start(response)
        response.close()

    def test_recommended_ciphers_with_real_connection(self) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(DEFAULT_CERTS["certfile"], DEFAULT_CERTS["keyfile"])

        def handler(listener: socket.socket) -> None:
            with listener.accept()[0] as raw:
                raw.settimeout(5)
                with context.wrap_socket(raw, server_side=True) as tls:
                    assert tls.recv(1) == b"x"
                    tls.sendall(b"y")

        self._start_server(handler)
        with socket.create_connection((self.host, self.port), timeout=5) as raw:
            with ssl_wrap_socket(
                raw,
                ca_certs=DEFAULT_CA,
                server_hostname="localhost",
                use_recommended_ciphers=True,
            ) as tls:
                tls.sendall(b"x")
                assert tls.recv(1) == b"y"
