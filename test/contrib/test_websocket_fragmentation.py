"""Tests for WebSocket frame fragmentation handling in next_payload().

When a WebSocket message spans multiple frames, wsproto emits TextMessage/BytesMessage
events with message_finished=False for each intermediate fragment and message_finished=True
only on the final fragment. next_payload() must buffer these fragments and return the
complete reassembled message.
"""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncGenerator, Generator
from unittest.mock import MagicMock

import pytest
from wsproto import ConnectionType, WSConnection
from wsproto.events import AcceptConnection, BytesMessage, Request, TextMessage

from urllib3.contrib.webextensions._async import AsyncWebSocketExtensionFromHTTP
from urllib3.contrib.webextensions.ws import WebSocketExtensionFromHTTP


# region Shared helpers


def _make_paired_server(client_protocol: WSConnection) -> WSConnection:
    """Complete a wsproto handshake and return the paired server WSConnection."""
    server = WSConnection(ConnectionType.SERVER)
    handshake_data = client_protocol.send(Request(host="localhost", target="/"))
    server.receive_data(handshake_data)
    for _ in server.events():
        pass  # consume Request

    accept_data = server.send(AcceptConnection())
    client_protocol.receive_data(accept_data)
    for _ in client_protocol.events():
        pass  # consume AcceptConnection

    return server


def _build_fragmented_frames(
    server: WSConnection,
    payload: str | bytes,
    chunk_size: int,
) -> list[bytes]:
    """Use a server WSConnection to produce fragmented WebSocket frames."""
    is_text = isinstance(payload, str)
    chunks = [payload[i : i + chunk_size] for i in range(0, len(payload), chunk_size)]
    frames: list[bytes] = []

    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        event: TextMessage | BytesMessage
        if isinstance(chunk, str):
            event = TextMessage(data=chunk, message_finished=is_last)
        else:
            event = BytesMessage(data=chunk, message_finished=is_last)
        frames.append(server.send(event))

    return frames


# endregion

# region Sync fakes


class FakePoliceOfficer:
    @contextmanager
    def borrow(self, response: Any) -> Generator[None, None, None]:
        yield


class FakeDSA:
    def __init__(self, frames: list[bytes]) -> None:
        self._frames = list(frames)
        self._index = 0

    def recv_extended(self, max_bytes: int | None) -> tuple[bytes, bool, bool]:
        if self._index >= len(self._frames):
            raise OSError("No more data")
        data = self._frames[self._index]
        self._index += 1
        return data, False, False

    def sendall(self, data: bytes) -> None:
        pass

    def close(self) -> None:
        pass


def _make_sync_ext(
    frames: list[bytes],
) -> tuple[WebSocketExtensionFromHTTP, WSConnection]:
    ext = WebSocketExtensionFromHTTP()
    server = _make_paired_server(ext._protocol)
    ext._dsa = FakeDSA(frames)  # type: ignore[assignment]
    ext._police_officer = FakePoliceOfficer()  # type: ignore[assignment]
    ext._response = MagicMock()
    return ext, server


# endregion

# region Async fakes


class AsyncFakePoliceOfficer:
    @asynccontextmanager
    async def borrow(self, response: Any) -> AsyncGenerator[None, None]:
        yield


class AsyncFakeDSA:
    def __init__(self, frames: list[bytes]) -> None:
        self._frames = list(frames)
        self._index = 0

    async def recv_extended(self, max_bytes: int | None) -> tuple[bytes, bool, bool]:
        if self._index >= len(self._frames):
            raise OSError("No more data")
        data = self._frames[self._index]
        self._index += 1
        return data, False, False

    async def sendall(self, data: bytes) -> None:
        pass

    async def close(self) -> None:
        pass


def _make_async_ext(
    frames: list[bytes],
) -> tuple[AsyncWebSocketExtensionFromHTTP, WSConnection]:
    ext = AsyncWebSocketExtensionFromHTTP()
    server = _make_paired_server(ext._protocol)
    ext._dsa = AsyncFakeDSA(frames)  # type: ignore[assignment]
    ext._police_officer = AsyncFakePoliceOfficer()  # type: ignore[assignment]
    ext._response = MagicMock()
    return ext, server


# endregion

# region Sync tests


class TestWebSocketFragmentation:
    """Verify next_payload() correctly reassembles fragmented WebSocket messages."""

    def test_fragmented_text_message(self) -> None:
        """A text message split across 3 frames should be fully reassembled."""
        full_message = "A" * 300
        ext, server = _make_sync_ext([])
        frames = _build_fragmented_frames(server, full_message, chunk_size=100)
        assert len(frames) == 3
        ext._dsa = FakeDSA(frames)  # type: ignore[assignment]

        assert ext.next_payload() == full_message

    def test_fragmented_bytes_message(self) -> None:
        """A binary message split across 4 frames should be fully reassembled."""
        full_message = b"\x00\xff" * 200  # 400 bytes
        ext, server = _make_sync_ext([])
        frames = _build_fragmented_frames(server, full_message, chunk_size=100)
        assert len(frames) == 4
        ext._dsa = FakeDSA(frames)  # type: ignore[assignment]

        assert ext.next_payload() == full_message

    def test_unfragmented_message_still_works(self) -> None:
        """A single-frame (unfragmented) message must still be returned correctly."""
        full_message = "Hello, World!"
        ext, server = _make_sync_ext([])
        frames = [server.send(TextMessage(data=full_message))]
        ext._dsa = FakeDSA(frames)  # type: ignore[assignment]

        assert ext.next_payload() == full_message

    def test_two_fragmented_messages_sequential(self) -> None:
        """Two separate fragmented messages read via sequential next_payload() calls."""
        msg1 = "First" * 50
        msg2 = "Second" * 50
        ext, server = _make_sync_ext([])
        frames = _build_fragmented_frames(
            server, msg1, chunk_size=50
        ) + _build_fragmented_frames(server, msg2, chunk_size=50)
        ext._dsa = FakeDSA(frames)  # type: ignore[assignment]

        assert ext.next_payload() == msg1
        assert ext.next_payload() == msg2


# endregion

# region Async tests


@pytest.mark.asyncio
class TestAsyncWebSocketFragmentation:
    """Verify async next_payload() correctly reassembles fragmented WebSocket messages."""

    async def test_fragmented_text_message(self) -> None:
        """A text message split across 3 frames should be fully reassembled."""
        full_message = "A" * 300
        ext, server = _make_async_ext([])
        frames = _build_fragmented_frames(server, full_message, chunk_size=100)
        assert len(frames) == 3
        ext._dsa = AsyncFakeDSA(frames)  # type: ignore[assignment]

        assert await ext.next_payload() == full_message

    async def test_fragmented_bytes_message(self) -> None:
        """A binary message split across 4 frames should be fully reassembled."""
        full_message = b"\x00\xff" * 200  # 400 bytes
        ext, server = _make_async_ext([])
        frames = _build_fragmented_frames(server, full_message, chunk_size=100)
        assert len(frames) == 4
        ext._dsa = AsyncFakeDSA(frames)  # type: ignore[assignment]

        assert await ext.next_payload() == full_message

    async def test_unfragmented_message_still_works(self) -> None:
        """A single-frame (unfragmented) message must still be returned correctly."""
        full_message = "Hello, World!"
        ext, server = _make_async_ext([])
        frames = [server.send(TextMessage(data=full_message))]
        ext._dsa = AsyncFakeDSA(frames)  # type: ignore[assignment]

        assert await ext.next_payload() == full_message

    async def test_two_fragmented_messages_sequential(self) -> None:
        """Two separate fragmented messages read via sequential next_payload() calls."""
        msg1 = "First" * 50
        msg2 = "Second" * 50
        ext, server = _make_async_ext([])
        frames = _build_fragmented_frames(
            server, msg1, chunk_size=50
        ) + _build_fragmented_frames(server, msg2, chunk_size=50)
        ext._dsa = AsyncFakeDSA(frames)  # type: ignore[assignment]

        assert await ext.next_payload() == msg1
        assert await ext.next_payload() == msg2


# endregion
