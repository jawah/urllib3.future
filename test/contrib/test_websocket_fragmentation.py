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

wsproto = pytest.importorskip("wsproto")

from wsproto import ConnectionType, WSConnection  # noqa: E402
from wsproto.events import (  # noqa: E402
    AcceptConnection,
    BytesMessage,
    Request,
    TextMessage,
)

from urllib3.contrib.webextensions._async import (  # noqa: E402
    AsyncWebSocketExtensionFromHTTP,
)
from urllib3.contrib.webextensions.ws import WebSocketExtensionFromHTTP  # noqa: E402


def _complete_handshake(client: WSConnection) -> WSConnection:
    """Drive a wsproto client/server handshake and return the server side."""
    server = WSConnection(ConnectionType.SERVER)
    server.receive_data(client.send(Request(host="localhost", target="/")))
    for _ in server.events():
        pass  # consume the Request event

    client.receive_data(server.send(AcceptConnection()))
    for _ in client.events():
        pass  # consume the AcceptConnection event

    return server


def _build_fragmented_frames(
    server: WSConnection,
    payload: str | bytes,
    chunk_size: int,
) -> list[bytes]:
    """Use a server WSConnection to produce fragmented WebSocket frames."""
    chunks = [payload[i : i + chunk_size] for i in range(0, len(payload), chunk_size)]
    frames: list[bytes] = []

    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        if isinstance(chunk, str):
            frames.append(
                server.send(TextMessage(data=chunk, message_finished=is_last))
            )
        else:
            frames.append(
                server.send(BytesMessage(data=chunk, message_finished=is_last))
            )

    return frames


class _FakePoliceOfficer:
    @contextmanager
    def borrow(self, response: Any) -> Generator[None, None, None]:
        yield

    def forget(self, response: Any) -> None:
        pass


class _FakeDSA:
    """Feeds pre-built raw frames one-by-one to the extension."""

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


def _wire_sync_ext(
    ext: WebSocketExtensionFromHTTP,
    frames: list[bytes],
) -> None:
    """Inject fake DSA/police/response into an already-handshaken extension."""
    ext._dsa = _FakeDSA(frames)  # type: ignore[assignment]
    ext._police_officer = _FakePoliceOfficer()  # type: ignore[assignment]
    ext._response = MagicMock()


class _AsyncFakePoliceOfficer:
    @asynccontextmanager
    async def borrow(self, response: Any) -> AsyncGenerator[None, None]:
        yield

    def forget(self, response: Any) -> None:
        pass


class _AsyncFakeDSA:
    """Async variant of _FakeDSA."""

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


def _wire_async_ext(
    ext: AsyncWebSocketExtensionFromHTTP,
    frames: list[bytes],
) -> None:
    """Inject fake DSA/police/response into an already-handshaken extension."""
    ext._dsa = _AsyncFakeDSA(frames)  # type: ignore[assignment]
    ext._police_officer = _AsyncFakePoliceOfficer()  # type: ignore[assignment]
    ext._response = MagicMock()


class TestWebSocketFragmentation:
    """Verify next_payload() correctly reassembles fragmented WebSocket messages."""

    def test_fragmented_text_message(self) -> None:
        """A text message split across 3 frames should be fully reassembled."""
        message = "A" * 300
        ext = WebSocketExtensionFromHTTP()
        server = _complete_handshake(ext._protocol)
        frames = _build_fragmented_frames(server, message, chunk_size=100)
        assert len(frames) == 3
        _wire_sync_ext(ext, frames)

        assert ext.next_payload() == message

    def test_fragmented_bytes_message(self) -> None:
        """A binary message split across 4 frames should be fully reassembled."""
        message = b"\x00\xff" * 200  # 400 bytes
        ext = WebSocketExtensionFromHTTP()
        server = _complete_handshake(ext._protocol)
        frames = _build_fragmented_frames(server, message, chunk_size=100)
        assert len(frames) == 4
        _wire_sync_ext(ext, frames)

        assert ext.next_payload() == message

    def test_unfragmented_message(self) -> None:
        """A single-frame message must still be returned correctly."""
        message = "Hello, World!"
        ext = WebSocketExtensionFromHTTP()
        server = _complete_handshake(ext._protocol)
        frames = [server.send(TextMessage(data=message))]
        _wire_sync_ext(ext, frames)

        assert ext.next_payload() == message

    def test_sequential_fragmented_messages(self) -> None:
        """Two fragmented messages read via sequential next_payload() calls."""
        msg1 = "First" * 50
        msg2 = "Second" * 50
        ext = WebSocketExtensionFromHTTP()
        server = _complete_handshake(ext._protocol)
        frames = _build_fragmented_frames(
            server, msg1, chunk_size=50
        ) + _build_fragmented_frames(server, msg2, chunk_size=50)
        _wire_sync_ext(ext, frames)

        assert ext.next_payload() == msg1
        assert ext.next_payload() == msg2


@pytest.mark.asyncio
class TestAsyncWebSocketFragmentation:
    """Verify async next_payload() correctly reassembles fragmented WebSocket messages."""

    async def test_fragmented_text_message(self) -> None:
        """A text message split across 3 frames should be fully reassembled."""
        message = "A" * 300
        ext = AsyncWebSocketExtensionFromHTTP()
        server = _complete_handshake(ext._protocol)
        frames = _build_fragmented_frames(server, message, chunk_size=100)
        assert len(frames) == 3
        _wire_async_ext(ext, frames)

        assert await ext.next_payload() == message

    async def test_fragmented_bytes_message(self) -> None:
        """A binary message split across 4 frames should be fully reassembled."""
        message = b"\x00\xff" * 200  # 400 bytes
        ext = AsyncWebSocketExtensionFromHTTP()
        server = _complete_handshake(ext._protocol)
        frames = _build_fragmented_frames(server, message, chunk_size=100)
        assert len(frames) == 4
        _wire_async_ext(ext, frames)

        assert await ext.next_payload() == message

    async def test_unfragmented_message(self) -> None:
        """A single-frame message must still be returned correctly."""
        message = "Hello, World!"
        ext = AsyncWebSocketExtensionFromHTTP()
        server = _complete_handshake(ext._protocol)
        frames = [server.send(TextMessage(data=message))]
        _wire_async_ext(ext, frames)

        assert await ext.next_payload() == message

    async def test_sequential_fragmented_messages(self) -> None:
        """Two fragmented messages read via sequential next_payload() calls."""
        msg1 = "First" * 50
        msg2 = "Second" * 50
        ext = AsyncWebSocketExtensionFromHTTP()
        server = _complete_handshake(ext._protocol)
        frames = _build_fragmented_frames(
            server, msg1, chunk_size=50
        ) + _build_fragmented_frames(server, msg2, chunk_size=50)
        _wire_async_ext(ext, frames)

        assert await ext.next_payload() == msg1
        assert await ext.next_payload() == msg2
