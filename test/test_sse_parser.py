"""Unit tests for the SSE event parser.

Exercises the parser in isolation (no HTTP server needed) by mocking the
underlying byte stream.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from urllib3.contrib.webextensions.sse import (
    ServerSentEvent,
    ServerSideEventExtensionFromHTTP,
)


def _make_ext(chunks: list[bytes]) -> ServerSideEventExtensionFromHTTP:
    """Build an SSE extension backed by a fake stream of byte chunks."""
    ext = ServerSideEventExtensionFromHTTP()
    ext._stream = iter(chunks)
    ext._response = MagicMock()
    return ext


@pytest.mark.parametrize(
    "chunks, expected_data",
    [
        pytest.param(
            [b"data: one\n\n", b"data: two\n\n"],
            ["one", "two"],
            id="one-event-per-chunk",
        ),
        pytest.param(
            [b'data: {"a":1}\n\ndata: {"b":2}\n\n'],
            ['{"a":1}', '{"b":2}'],
            id="two-events-in-one-chunk",
        ),
        pytest.param(
            [b"data: first\n\ndata: second\n\ndata: third\n\n"],
            ["first", "second", "third"],
            id="three-events-in-one-chunk",
        ),
        pytest.param(
            [b"data: hel", b"lo\n\n"],
            ["hello"],
            id="event-split-across-chunks",
        ),
        pytest.param(
            [b"data: first\n\ndata: sec", b"ond\n\n"],
            ["first", "second"],
            id="mixed-bundled-and-split",
        ),
        pytest.param(
            [b"data: one\r\n\r\ndata: two\r\n\r\n"],
            ["one", "two"],
            id="crlf-separator",
        ),
    ],
)
def test_sse_parser(chunks: list[bytes], expected_data: list[str]) -> None:
    ext = _make_ext(chunks)
    events = []
    while (ev := ext.next_payload()) is not None:
        assert isinstance(ev, ServerSentEvent)
        events.append(ev.data)
    assert events == expected_data


def test_last_event_id_preserved_across_multi_event_chunk() -> None:
    ext = _make_ext([b"id: 42\ndata: first\n\ndata: second\n\n"])
    ev1 = ext.next_payload()
    ev2 = ext.next_payload()
    assert isinstance(ev1, ServerSentEvent) and ev1.id == "42"
    assert isinstance(ev2, ServerSentEvent) and ev2.id == "42"