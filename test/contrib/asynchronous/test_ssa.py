from __future__ import annotations

import asyncio
import socket
import ssl
import sys
from unittest.mock import Mock

import pytest
import trustme

from urllib3.contrib.ssa import AsyncSocket


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
            assert replacement.fileno() == old_fd
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
