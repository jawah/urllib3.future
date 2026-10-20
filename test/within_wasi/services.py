from __future__ import annotations

import json
import select
import socket
import socketserver
import ssl
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import trustme


class TLSHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        peer = self.connection.getpeercert()
        body = json.dumps(
            {
                "path": self.path,
                "client_certificate": bool(peer),
                "tls_version": self.connection.version(),
            }
        ).encode("ascii")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return None


class SOCKSHandler(socketserver.BaseRequestHandler):
    def _read_exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self.request.recv(size - len(data))
            if not chunk:
                raise EOFError
            data.extend(chunk)
        return bytes(data)

    def handle(self) -> None:
        client = self.request
        try:
            version, method_count = self._read_exact(2)
            methods = self._read_exact(method_count)
        except EOFError:
            return
        if version != 5 or 0 not in methods:
            return
        client.sendall(b"\x05\x00")

        try:
            version, command, _, address_type = self._read_exact(4)
        except EOFError:
            return
        if version != 5 or command != 1:
            return
        if address_type == 1:
            host = socket.inet_ntoa(self._read_exact(4))
        elif address_type == 3:
            host = self._read_exact(self._read_exact(1)[0]).decode("ascii")
        elif address_type == 4:
            host = socket.inet_ntop(socket.AF_INET6, self._read_exact(16))
        else:
            return
        port = struct.unpack("!H", self._read_exact(2))[0]
        if host in {"httpbin.local", "alt.httpbin.local"}:
            host = "127.0.0.1"

        try:
            upstream = socket.create_connection((host, port), timeout=5)
        except OSError:
            client.sendall(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
            return

        client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        sockets = [client, upstream]
        try:
            while True:
                readable, _, _ = select.select(sockets, [], [], 5)
                if not readable:
                    continue
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    destination = upstream if source is client else client
                    destination.sendall(data)
        finally:
            upstream.close()


class ThreadingSOCKSServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class SocketEdgeHandler(socketserver.BaseRequestHandler):
    """A bounded native peer for guest socket error and EOF cases."""

    def handle(self) -> None:
        self.request.settimeout(10)
        context = getattr(self.server, "tls_context", None)
        if context is not None:
            with context.wrap_socket(self.request, server_side=True) as peer:
                self._serve(peer)
        else:
            self._serve(self.request)

    def _serve(self, peer: socket.socket) -> None:
        if getattr(self.server, "mode", "plain") == "handshake-eof":
            peer.recv(65536)
            return
        line = bytearray()
        while not line.endswith(b"\n"):
            data = peer.recv(1)
            if not data:
                return
            line.extend(data)
        if line == b"reset\n":
            peer.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
            peer.close()
        elif line == b"corrupt\n":
            # Bypass TLS framing after the genuine handshake to send a bad record.
            with socket.socket(fileno=peer.detach()) as raw:
                raw.sendall(b"\x17\x03\x03\x00\x10" + bytes(16))
        elif line == b"half-close\n":
            while peer.recv(4096):
                pass
            peer.sendall(b"after eof")
        elif line == b"zero-read\n":
            # The guest must finish recv(0) before it sends this second command.
            if peer.recv(1) == b"!":
                peer.sendall(b"ready")
        else:
            peer.sendall(b"hello")


class SocketEdgeServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    mode = "plain"
    tls_context: ssl.SSLContext | None = None


class IPv6SocketEdgeServer(SocketEdgeServer):
    address_family = socket.AF_INET6


class NativeServices:
    def __init__(self, fixtures: Path) -> None:
        fixtures.mkdir(parents=True, exist_ok=True)
        ca = trustme.CA()
        server_cert = ca.issue_cert("localhost", "127.0.0.1", "httpbin.local")
        client_cert = ca.issue_cert("wasi-client")

        ca.cert_pem.write_to_path(fixtures / "native-ca.pem")
        server_cert.cert_chain_pems[0].write_to_path(fixtures / "server.pem")
        server_cert.private_key_pem.write_to_path(fixtures / "server.key")
        client_cert.cert_chain_pems[0].write_to_path(fixtures / "client.pem")
        client_cert.private_key_pem.write_to_path(fixtures / "client.key")

        traefik_ca = Path("rootCA.pem").read_bytes()
        native_ca = (fixtures / "native-ca.pem").read_bytes()
        (fixtures / "root-ca.pem").write_bytes(native_ca + traefik_ca)

        proxy_ca = Path("dummyserver/certs/cacert.pem").read_bytes()
        (fixtures / "combined-ca.pem").write_bytes(
            (fixtures / "root-ca.pem").read_bytes() + proxy_ca
        )

        self.servers: list[Any] = []
        self.threads: list[threading.Thread] = []
        self._start_tls_server(fixtures, 8444, require_client=True)
        self._start_tls_server(fixtures, 8445, tls12_only=True)

        socks = ThreadingSOCKSServer(("127.0.0.1", 19080), SOCKSHandler)
        self.servers.append(socks)
        self._start_thread(socks.serve_forever, "wasi-socks5")

        # Keep this port reserved without listening, so refusal has no port-reuse race.
        self.refused = socket.socket()
        self.refused.bind(("127.0.0.1", 0))
        endpoints: dict[str, Any] = {"refused": self.refused.getsockname()}
        for name, host, server_class in (
            ("plain", "127.0.0.1", SocketEdgeServer),
            ("handshake-eof", "127.0.0.1", SocketEdgeServer),
            ("tls", "127.0.0.1", SocketEdgeServer),
            ("ipv6", "::1", IPv6SocketEdgeServer),
        ):
            try:
                server = server_class((host, 0), SocketEdgeHandler)
            except OSError:
                if name != "ipv6":
                    raise
                endpoints[name] = None
                continue
            server.mode = name
            if name == "tls":
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(
                    fixtures / "server.pem", fixtures / "server.key"
                )
                server.tls_context = context
            self.servers.append(server)
            endpoints[name] = server.server_address[:2]
            self._start_thread(server.serve_forever, f"wasi-edge-{name}")
        (fixtures / "socket-edges.json").write_text(json.dumps(endpoints))

    def _start_tls_server(
        self,
        fixtures: Path,
        port: int,
        *,
        require_client: bool = False,
        tls12_only: bool = False,
    ) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(fixtures / "server.pem", fixtures / "server.key")
        if require_client:
            context.verify_mode = ssl.CERT_REQUIRED
            context.load_verify_locations(fixtures / "native-ca.pem")
        if tls12_only:
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.maximum_version = ssl.TLSVersion.TLSv1_2

        server = ThreadingHTTPServer(("127.0.0.1", port), TLSHandler)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        self.servers.append(server)
        self._start_thread(server.serve_forever, f"wasi-tls-{port}")

    def _start_thread(self, target: Any, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self.threads.append(thread)

    def close(self) -> None:
        self.refused.close()
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)
