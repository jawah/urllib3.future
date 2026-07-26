from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

import urllib3.backend._async.hface as async_hface
import urllib3.backend.hface as hface
from urllib3.contrib.hface.events import HandshakeCompleted


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self) -> None:
        self.now += 1.0


class _DribbleSocket:
    def __init__(self, clock: _Clock) -> None:
        self._clock = clock
        self._timeout: float | None = None
        self.timeouts: list[float] = []

    def settimeout(self, timeout: float | None) -> None:
        self._timeout = timeout
        if timeout is not None:
            self.timeouts.append(timeout)

    def gettimeout(self) -> float | None:
        return self._timeout

    def recv(self, size: int) -> bytes:
        self._clock.advance()
        return b"\x00"

    def sendall(self, data: object) -> None:
        pass


class _AsyncDribbleSocket(_DribbleSocket):
    async def recv(self, size: int) -> bytes:
        self._clock.advance()
        return b"\x00"

    async def sendall(self, data: object) -> None:
        pass


class _StalledProtocol:
    def has_pending_event(self, stream_id: int | None = None) -> bool:
        return False

    def bytes_to_send(self) -> bytes:
        return b""

    def next_timer(self) -> None:
        return None

    def events(self, stream_id: int | None = None) -> tuple[()]:
        return ()

    def bytes_received(self, data: object) -> None:
        pass

    def exceptions(self) -> tuple[()]:
        return ()


def test_sync_handshake_deadline_caps_total_not_each_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(hface, "time", SimpleNamespace(monotonic=clock))

    backend = hface.HfaceBackend.__new__(hface.HfaceBackend)
    sock = _DribbleSocket(clock)
    backend.sock = sock
    backend._protocol = _StalledProtocol()
    backend.blocksize = 4096
    backend._svn = None
    backend._dgram_gso_enabled = False
    backend._dgram_gro_enabled = False
    backend._response = None

    with pytest.raises(socket.timeout):
        backend._HfaceBackend__exchange_until(  # type: ignore[attr-defined]
            HandshakeCompleted,
            deadline=3.0,
        )

    assert sock.timeouts == [3.0, 2.0, 1.0]


@pytest.mark.asyncio
async def test_async_handshake_deadline_caps_total_not_each_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(async_hface, "time", SimpleNamespace(monotonic=clock))

    backend = async_hface.AsyncHfaceBackend.__new__(async_hface.AsyncHfaceBackend)
    sock = _AsyncDribbleSocket(clock)
    backend.sock = sock
    backend._protocol = _StalledProtocol()
    backend.blocksize = 4096
    backend._svn = None
    backend._response = None
    backend._recv_size_ema = 0.0

    with pytest.raises(socket.timeout):
        await backend._AsyncHfaceBackend__exchange_until(  # type: ignore[attr-defined]
            HandshakeCompleted,
            deadline=3.0,
        )

    assert sock.timeouts == [3.0, 2.0, 1.0]
