from __future__ import annotations

import pytest

pytest.importorskip("wsproto")
from wsproto.events import (  # noqa: E402
    TextMessage,
)

from test.contrib.websocket_helpers import (
    _build_fragmented_frames,
    _complete_handshake,
    _wire_async_ext,
)
from urllib3.contrib.webextensions._async import (  # noqa: E402
    AsyncWebSocketExtensionFromHTTP,
)


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
