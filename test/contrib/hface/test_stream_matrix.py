from __future__ import annotations

from urllib3.contrib.hface._stream_matrix import StreamMatrix
from urllib3.contrib.hface.events import (
    ConnectionTerminated,
    DataReceived,
    HandshakeCompleted,
    HeadersReceived,
)


def test_single_ev_in_matrix() -> None:
    sm = StreamMatrix()

    assert sm.count() == 0

    sm.append(HandshakeCompleted("h2"))

    assert sm.count() == 1

    assert isinstance(sm.popleft(), HandshakeCompleted)


def test_flat_stream_matrix() -> None:
    sm = StreamMatrix()

    sm.append(HandshakeCompleted("h2"))
    sm.append(HeadersReceived(4, (), True))
    sm.append(HeadersReceived(7, (), False))
    sm.append(DataReceived(7, b"foo", False))
    sm.append(DataReceived(7, b"", True))
    sm.append(ConnectionTerminated())

    assert isinstance(sm.popleft(), HandshakeCompleted)
    cursor_ev = sm.popleft()

    assert isinstance(cursor_ev, HeadersReceived)
    assert cursor_ev.stream_id == 4

    cursor_ev = sm.popleft()
    assert isinstance(cursor_ev, HeadersReceived)
    assert cursor_ev.stream_id == 7

    cursor_ev = sm.popleft()
    assert isinstance(cursor_ev, DataReceived)
    assert cursor_ev.stream_id == 7
    assert cursor_ev.data == b"foo"

    cursor_ev = sm.popleft()
    assert isinstance(cursor_ev, DataReceived)
    assert cursor_ev.stream_id == 7
    assert cursor_ev.data == b""

    assert isinstance(sm.popleft(), ConnectionTerminated)


def test_non_flat_stream_matrix() -> None:
    sm = StreamMatrix()

    sm.append(HandshakeCompleted("h2"))
    sm.append(HeadersReceived(4, (), True))
    sm.append(HeadersReceived(7, (), False))
    sm.append(DataReceived(7, b"foo", False))
    sm.append(DataReceived(7, b"", True))
    sm.append(ConnectionTerminated())

    assert isinstance(sm.popleft(stream_id=4), HandshakeCompleted)
    cursor_ev = sm.popleft(stream_id=7)

    assert isinstance(cursor_ev, HeadersReceived)
    assert cursor_ev.stream_id == 7

    cursor_ev = sm.popleft(stream_id=7)
    assert isinstance(cursor_ev, DataReceived)
    assert cursor_ev.stream_id == 7
    assert cursor_ev.data == b"foo"

    cursor_ev = sm.popleft(stream_id=7)
    assert isinstance(cursor_ev, DataReceived)
    assert cursor_ev.stream_id == 7
    assert cursor_ev.data == b""

    cursor_ev = sm.popleft()

    assert isinstance(cursor_ev, HeadersReceived)
    assert cursor_ev.stream_id == 4

    assert isinstance(sm.popleft(), ConnectionTerminated)

    assert sm.popleft(stream_id=7) is None


def test_extend_matrix_stream() -> None:
    sm = StreamMatrix()

    sm.extend(
        [
            HandshakeCompleted("h2"),
        ]
    )

    assert sm.streams == []

    sm.extend(
        [
            HeadersReceived(4, (), True),
        ]
    )

    assert sm.streams == [4]

    sm.extend(
        [
            HeadersReceived(7, (), False),
            DataReceived(7, b"foo", False),
            DataReceived(7, b"", True),
            ConnectionTerminated(),
        ]
    )

    assert sm.streams == [4, 7]

    assert sm.count() == 6

    assert isinstance(sm.popleft(stream_id=4), HandshakeCompleted)
    cursor_ev = sm.popleft(stream_id=7)

    assert isinstance(cursor_ev, HeadersReceived)
    assert cursor_ev.stream_id == 7

    cursor_ev = sm.popleft(stream_id=7)
    assert isinstance(cursor_ev, DataReceived)
    assert cursor_ev.stream_id == 7
    assert cursor_ev.data == b"foo"

    cursor_ev = sm.popleft(stream_id=7)
    assert isinstance(cursor_ev, DataReceived)
    assert cursor_ev.stream_id == 7
    assert cursor_ev.data == b""

    cursor_ev = sm.popleft()

    assert isinstance(cursor_ev, HeadersReceived)
    assert cursor_ev.stream_id == 4

    assert isinstance(sm.popleft(), ConnectionTerminated)
