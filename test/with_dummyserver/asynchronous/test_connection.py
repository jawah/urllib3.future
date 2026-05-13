from __future__ import annotations

import time

import pytest

from dummyserver.testcase import HTTPDummyServerTestCase as server
from urllib3 import AsyncHTTPConnectionPool


@pytest.mark.asyncio
async def test_is_connected_false_after_keepalive_delay_elapsed() -> None:
    """Async mirror of the keepalive-delay expiry branch in
    ``AsyncHTTPConnection.is_connected`` (see
    ``src/urllib3/_async/connection.py``). Once the configured keepalive
    window has elapsed the pool must consider the connection stale without
    consulting the kernel.
    """
    server.setup_class()
    try:
        async with AsyncHTTPConnectionPool(server.host, server.port) as pool:
            conn = await pool._get_conn()
            await conn.connect()

            assert conn.is_connected is True

            conn._keepalive_delay = 0.001
            conn._connected_at = time.monotonic() - 10.0

            assert conn.is_connected is False

            await conn.close()
    finally:
        server.teardown_class()


@pytest.mark.asyncio
async def test_is_connected_false_when_socket_fileno_invalid() -> None:
    """Async mirror covering ``_async/connection.py:308`` -- the
    ``sock.fileno() == -1`` early-return False branch.
    """
    server.setup_class()
    try:
        async with AsyncHTTPConnectionPool(server.host, server.port) as pool:
            conn = await pool._get_conn()
            await conn.connect()

            assert conn.is_connected is True

            assert conn.sock is not None
            conn.sock.close()

            assert conn.is_connected is False

            await conn.close()
    finally:
        server.teardown_class()
