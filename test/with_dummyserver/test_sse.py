from __future__ import annotations

import asyncio
import codecs
import inspect
import socket
import typing
from threading import Event

import pytest

from dummyserver.testcase import SocketDummyServerTestCase, consume_socket
from urllib3 import AsyncPoolManager, PoolManager
from urllib3.contrib.webextensions.sse import (
    ServerSentEvent,
    ServerSideEventExtensionFromHTTP,
)
from urllib3.contrib.webextensions._async.sse import (
    AsyncServerSideEventExtensionFromHTTP,
)


async def call(fn: typing.Any, *args: typing.Any, **kwargs: typing.Any) -> typing.Any:
    result = fn(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


# WHATWG HTML 9.2.5–9.2.6: event-stream framing and interpretation.
CASES = [
    pytest.param(
        b"data: first\ndata: second\n\n",
        [("message", "first\nsecond", "", None)],
        id="issue-439",
    ),
    pytest.param(
        b"data: first\ndata:\ndata: third\ndata:\n\n",
        [("message", "first\n\nthird\n", "", None)],
        id="empty-data-lines",
    ),
    pytest.param(b"data\n\n", [("message", "", "", None)], id="colonless-data"),
    pytest.param(
        b"data: first\rdata: second\r\r",
        [("message", "first\nsecond", "", None)],
        id="cr-lines",
    ),
    pytest.param(
        b"data: first\r\ndata: second\n\r",
        [("message", "first\nsecond", "", None)],
        id="mixed-lines",
    ),
    pytest.param(
        b"data: first\r\rdata: second\n\n",
        [("message", "first", "", None), ("message", "second", "", None)],
        id="mixed-events",
    ),
    pytest.param(
        b"\xef\xbb\xbfdata: first\n\ndata: second\n\n",
        [("message", "first", "", None), ("message", "second", "", None)],
        id="leading-bom",
    ),
    pytest.param(
        "data: a\ufeffb\n\n".encode(),
        [("message", "a\ufeffb", "", None)],
        id="embedded-bom",
    ),
    pytest.param(
        "data: a\u0085b\u2028c\u2029d\vnext\ffinal\n\n".encode(),
        [("message", "a\u0085b\u2028c\u2029d\vnext\ffinal", "", None)],
        id="unicode-is-not-line-ending",
    ),
    pytest.param(
        b"data: before\xffafter\n\n",
        [("message", "before\ufffdafter", "", None)],
        id="invalid-utf8",
    ),
    pytest.param(
        b"data: ok\n\ndata: incomplete \xe2\x82",
        [("message", "ok", "", None)],
        id="incomplete-utf8-at-eof",
    ),
    pytest.param(b"data: discard\n", [], id="no-final-blank-line"),
    pytest.param(b"\xef\xbb\xbf", [], id="only-bom"),
    pytest.param(
        b"id: cursor\ndata: first\n\nid:\ndata: second\n\ndata: third\n\n",
        [
            ("message", "first", "cursor", None),
            ("message", "second", "", None),
            ("message", "third", "", None),
        ],
        id="empty-id-reset",
    ),
    pytest.param(
        b"id: cursor\ndata: first\n\nid\ndata: second\n\ndata: third\n\n",
        [
            ("message", "first", "cursor", None),
            ("message", "second", "", None),
            ("message", "third", "", None),
        ],
        id="colonless-id-reset",
    ),
    pytest.param(
        b"id: cursor\ndata: first\n\nid: bad\x00id\ndata: second\n\n",
        [("message", "first", "cursor", None), ("message", "second", "cursor", None)],
        id="invalid-id-is-ignored",
    ),
    pytest.param(
        b": comment\nData: ignored\nunknown: ignored\n\ndata:  space\ndata:\ttab\ndata:x: y\n\n",
        [("message", " space\n\ttab\nx: y", "", None)],
        id="field-whitespace-and-comments",
    ),
    pytest.param(
        b"event: first\nevent: second\nretry: 1500\nretry: invalid\ndata: payload\n\ndata: next\n\n",
        [("second", "payload", "", 1500), ("message", "next", "", None)],
        id="field-state",
    ),
    # Preserve this low-level API's existing tolerance and metadata-only payloads.
    pytest.param(
        b"retry: +0010\nevent: update\n\nid: cursor\n\ndata: body\n\n",
        [
            ("update", "", "", 10),
            ("message", "", "cursor", None),
            ("message", "body", "cursor", None),
        ],
        id="permissive-metadata",
    ),
]


class TestSSEParsing(SocketDummyServerTestCase):
    def serve_events(self, body: bytes) -> None:
        def handler(listener: socket.socket) -> None:
            with listener.accept()[0] as sock:
                sock.settimeout(5)
                consume_socket(sock)
                sock.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode()
                    + body
                )

        self._start_server(handler)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
    @pytest.mark.parametrize("raw", [False, True])
    async def test_large_event_and_following_event(
        self, asynchronous: bool, raw: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = "x" * 65536
        event_text = "data: " + payload + "\n\n"
        self.serve_events(codecs.BOM_UTF8 + event_text.encode() + b"data: tail\n\n")
        decode = codecs.utf_8_decode
        views = 0

        def observe_decode(data: typing.Any, *args: typing.Any) -> typing.Any:
            nonlocal views
            if isinstance(data, memoryview):
                assert data.readonly
                views += 1
            return decode(data, *args)

        monkeypatch.setattr(codecs, "utf_8_decode", observe_decode)
        manager = (AsyncPoolManager if asynchronous else PoolManager)(
            timeout=5, retries=False, background_watch_delay=None
        )
        response = None
        try:
            response = await call(
                manager.urlopen, "GET", f"psse://{self.host}:{self.port}/"
            )
            extension = response.extension
            assert extension is not None
            event = await call(extension.next_payload, raw=raw)
            if raw:
                assert event == event_text
            else:
                assert isinstance(event, ServerSentEvent) and event.data == payload
                assert views > 0
            tail = await call(extension.next_payload)
            assert isinstance(tail, ServerSentEvent) and tail.data == "tail"
            assert await call(extension.next_payload) is None
            assert extension._buffer == b""
            assert extension._buffer_start == 0
        finally:
            if response is not None:
                await call(response.close)
            await call(manager.clear)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
    async def test_reused_extension_starts_with_fresh_text(
        self, asynchronous: bool
    ) -> None:
        extension = (
            AsyncServerSideEventExtensionFromHTTP
            if asynchronous
            else ServerSideEventExtensionFromHTTP
        )()
        manager = (AsyncPoolManager if asynchronous else PoolManager)(
            timeout=5, retries=False, background_watch_delay=None
        )
        try:
            for body, expected in (
                (b"id: cursor\ndata: first\n\ndata: stale", "first"),
                (b"\xef\xbb\xbfdata: second\n\n", "second"),
            ):
                self.serve_events(body)
                response = await call(
                    manager.urlopen,
                    "GET",
                    f"http://{self.host}:{self.port}/",
                    extension=extension,
                    preload_content=False,
                )
                try:
                    event = await call(extension.next_payload)
                    assert isinstance(event, ServerSentEvent)
                    assert event.data == expected and event.id == "cursor"
                    # Close before consuming the incomplete tail, then reuse the extension.
                finally:
                    await call(response.close)
        finally:
            await call(manager.clear)

    @pytest.mark.asyncio
    async def test_close_interrupts_pending_read(self) -> None:
        def handler(listener: socket.socket) -> None:
            with listener.accept()[0] as sock:
                sock.settimeout(5)
                consume_socket(sock)
                sock.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n"
                )
                assert sock.recv(1) == b""

        self._start_server(handler)
        async with AsyncPoolManager(
            timeout=5, retries=False, background_watch_delay=None
        ) as manager:
            response = await manager.urlopen("GET", f"psse://{self.host}:{self.port}/")
            extension = response.extension
            assert isinstance(extension, AsyncServerSideEventExtensionFromHTTP)
            stream = extension._stream
            assert stream is not None
            reading = asyncio.Event()

            async def observed_stream() -> typing.AsyncGenerator[bytes, None]:
                reading.set()
                async for chunk in stream:
                    yield chunk

            extension._stream = observed_stream()
            reader = asyncio.create_task(extension.next_payload())
            try:
                await asyncio.wait_for(reading.wait(), 5)
                await asyncio.wait_for(extension.close(), 5)
                assert await asyncio.wait_for(reader, 5) is None
                assert extension.closed
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                await response.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
    @pytest.mark.parametrize("blocksize", [1, 65536])
    @pytest.mark.parametrize("body, expected", CASES)
    async def test_event_stream(
        self,
        asynchronous: bool,
        blocksize: int,
        body: bytes,
        expected: list[tuple[str, str, str, int | None]],
    ) -> None:
        self.serve_events(body)
        manager = (AsyncPoolManager if asynchronous else PoolManager)(
            blocksize=blocksize, timeout=5, retries=False, background_watch_delay=None
        )
        response = None
        try:
            response = await call(
                manager.urlopen, "GET", f"psse://{self.host}:{self.port}/"
            )
            extension = response.extension
            assert extension is not None
            events = []
            while not extension.closed:
                event = await call(extension.next_payload)
                if event is not None:
                    assert isinstance(event, ServerSentEvent)
                    events.append((event.event, event.data, event.id, event.retry))
            assert events == expected
        finally:
            if response is not None:
                await call(response.close)
            await call(manager.clear)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
    @pytest.mark.parametrize("blocksize", [1, 65536])
    @pytest.mark.parametrize("ending", [b"\n", b"\r\n", b"\r", b"\n\r"])
    async def test_raw_keeps_original_event_text(
        self,
        asynchronous: bool,
        blocksize: int,
        ending: bytes,
    ) -> None:
        body = b"data: first\r\ndata: second" + ending + ending
        self.serve_events(b"\xef\xbb\xbf\r\n" + body)
        manager = (AsyncPoolManager if asynchronous else PoolManager)(
            blocksize=blocksize, timeout=5, retries=False, background_watch_delay=None
        )
        response = None
        try:
            response = await call(
                manager.urlopen, "GET", f"psse://{self.host}:{self.port}/"
            )
            extension = response.extension
            assert extension is not None
            raw = await call(extension.next_payload, raw=True)
            # For LF/CR mixed endings, the earliest blank line terminates the event.
            expected = body if ending != b"\n\r" else body[:-1]
            assert raw == expected.decode()
            assert await call(extension.next_payload) is None
        finally:
            if response is not None:
                await call(response.close)
            await call(manager.clear)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
    async def test_lone_cr_dispatches_before_peer_closes(
        self, asynchronous: bool
    ) -> None:
        release = Event()

        def handler(listener: socket.socket) -> None:
            with listener.accept()[0] as sock:
                sock.settimeout(5)
                consume_socket(sock)
                sock.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n9\r\ndata: x\r\r\r\n"
                )
                assert release.wait(10)
                sock.sendall(b"0\r\n\r\n")

        self._start_server(handler)
        manager = (AsyncPoolManager if asynchronous else PoolManager)(
            timeout=5, retries=False, background_watch_delay=None
        )
        response = None
        try:
            response = await call(
                manager.urlopen, "GET", f"psse://{self.host}:{self.port}/"
            )
            assert response.extension is not None
            event = await call(response.extension.next_payload)
            assert isinstance(event, ServerSentEvent) and event.data == "x"
            release.set()
            assert await call(response.extension.next_payload) is None
        finally:
            release.set()
            if response is not None:
                await call(response.close)
            await call(manager.clear)
