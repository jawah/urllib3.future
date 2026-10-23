from __future__ import annotations

import socket
import typing
from types import SimpleNamespace
from unittest import mock

import pytest

from urllib3._async.connection import AsyncHTTPConnection
from urllib3.backend import HttpVersion
from urllib3.backend._async.hface import AsyncHfaceBackend


class TestConnection:
    @pytest.mark.asyncio
    async def test_async_new_conn_receives_ech_config(self) -> None:
        ech_config = b"ech-config"
        sock = mock.MagicMock(type=socket.SOCK_STREAM)
        resolver = mock.MagicMock()

        async def create_connection(*args: typing.Any, **kwargs: typing.Any) -> object:
            kwargs["ech_config_hook"](ech_config)
            return sock

        resolver.create_connection.side_effect = create_connection
        conn = AsyncHTTPConnection("example.com", resolver=resolver)

        assert await conn._new_conn() is sock
        assert conn._ech_config == ech_config

    @pytest.mark.asyncio
    async def test_async_alt_svc_probe_waits_for_successful_response(self) -> None:
        conn = AsyncHfaceBackend("example.com", disabled_svn={HttpVersion.h3})
        conn.sock = mock.MagicMock()
        conn._svn = HttpVersion.h11
        probe = mock.Mock(side_effect=[None, ("", 443)])
        close_count = 0

        async def close() -> None:
            nonlocal close_count
            close_count += 1

        with mock.patch.object(conn, "_AsyncHfaceBackend__altsvc_probe", probe):
            with mock.patch.object(conn, "close", new=close):
                conn._response = SimpleNamespace(status=401)  # type: ignore[assignment]
                await conn._upgrade()
                conn._response = SimpleNamespace(status=200)  # type: ignore[assignment]
                await conn._upgrade()
                await conn._upgrade()

        assert probe.call_count == 2
        assert conn._svn is HttpVersion.h2
        assert close_count == 1

    @pytest.mark.asyncio
    async def test_async_successful_alt_svc_miss_is_checked_once(self) -> None:
        conn = AsyncHfaceBackend("example.com", disabled_svn={HttpVersion.h3})
        conn.sock = mock.MagicMock()
        conn._svn = HttpVersion.h11
        conn._response = SimpleNamespace(status=200)  # type: ignore[assignment]
        probe = mock.Mock(return_value=None)

        with mock.patch.object(conn, "_AsyncHfaceBackend__altsvc_probe", probe):
            await conn._upgrade()
            await conn._upgrade()

        probe.assert_called_once_with(svc="h2")
