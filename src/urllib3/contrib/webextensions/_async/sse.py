from __future__ import annotations

import asyncio
import codecs
import typing

if typing.TYPE_CHECKING:
    from ...._async.response import AsyncHTTPResponse

from ....backend import HttpVersion
from ..sse import ServerSentEvent
from .protocol import AsyncExtensionFromHTTP


class AsyncServerSideEventExtensionFromHTTP(AsyncExtensionFromHTTP):
    def __init__(self) -> None:
        super().__init__()

        self._next_value_task: asyncio.Task[bytes] | None = None
        self._last_event_id: str | None = None
        self._buffer: str = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._stream: typing.AsyncGenerator[bytes, None] | None = None

    @staticmethod
    def supported_svn() -> set[HttpVersion]:
        return {HttpVersion.h11, HttpVersion.h2, HttpVersion.h3}

    @staticmethod
    def implementation() -> str:
        return "native"

    @property
    def urlopen_kwargs(self) -> dict[str, typing.Any]:
        return {"preload_content": False}

    async def close(self) -> None:
        if self._stream is not None and self._response is not None:
            if self._next_value_task is not None:
                self._next_value_task.cancel()
                await self._next_value_task

            await self._stream.aclose()
            if (
                self._response._fp is not None
                and self._police_officer is not None
                and hasattr(self._response._fp, "abort")
            ):
                async with self._police_officer.borrow(self._response):
                    await self._response._fp.abort()
            self._stream = None
            self._response = None
            self._police_officer = None

    @property
    def closed(self) -> bool:
        return self._stream is None

    async def start(self, response: AsyncHTTPResponse) -> None:
        await super().start(response)

        self._stream = response.stream(-1, decode_content=True)

    def headers(self, http_version: HttpVersion) -> dict[str, str]:
        return {"accept": "text/event-stream", "cache-control": "no-store"}

    @typing.overload
    async def next_payload(self, *, raw: typing.Literal[True] = True) -> str | None: ...

    @typing.overload
    async def next_payload(
        self, *, raw: typing.Literal[False] = False
    ) -> ServerSentEvent | None: ...

    async def next_payload(self, *, raw: bool = False) -> ServerSentEvent | str | None:
        """Unpack the next received message/payload from remote."""
        if self._response is None or self._stream is None:
            raise OSError("The HTTP extension is closed or uninitialized")

        while True:
            # Read chunks until the buffer contains at least one complete event
            # (terminated by a blank line: \n\n or \r\n\r\n).
            while "\n\n" not in self._buffer and "\r\n\r\n" not in self._buffer:
                try:
                    self._next_value_task = asyncio.create_task(
                        self._stream.__anext__()
                    )
                    chunk = await self._next_value_task
                    self._buffer += self._decoder.decode(chunk)
                except asyncio.CancelledError:
                    return None
                except StopAsyncIteration:
                    last_chunk = self._decoder.decode(b"", final=True)
                    if not last_chunk:
                        await self._stream.aclose()
                        self._stream = None
                        self._decoder.reset()
                        return None
                    self._buffer += last_chunk
                finally:
                    self._next_value_task = None

            # Locate the first event boundary.
            lf = self._buffer.find("\n\n")
            crlf = self._buffer.find("\r\n\r\n")

            if crlf != -1 and (lf == -1 or crlf < lf):
                boundary, sep = crlf, "\r\n\r\n"
            else:
                boundary, sep = lf, "\n\n"

            event_text = self._buffer[:boundary]
            self._buffer = self._buffer[boundary + len(sep) :]

            kwargs: dict[str, typing.Any] = {}

            for line in event_text.splitlines():
                if not line:
                    continue
                key, _, value = line.partition(":")
                if key not in {"event", "data", "retry", "id"}:
                    continue
                if value.startswith(" "):
                    value = value[1:]
                if key == "id":
                    if "\u0000" in value:
                        continue
                if key == "retry":
                    try:
                        value = int(value)  # type: ignore[assignment]
                    except (ValueError, TypeError):
                        continue
                kwargs[key] = value

            if not kwargs:
                continue

            if "id" not in kwargs and self._last_event_id is not None:
                kwargs["id"] = self._last_event_id

            event = ServerSentEvent(**kwargs)

            if event.id:
                self._last_event_id = event.id

            if raw is True:
                return event_text + sep

            return event

    async def send_payload(self, buf: str | bytes) -> None:
        """Dispatch a buffer to remote."""
        raise NotImplementedError("SSE is only one-way. Sending is forbidden.")

    @staticmethod
    def supported_schemes() -> set[str]:
        return {"sse", "psse"}

    @staticmethod
    def scheme_to_http_scheme(scheme: str) -> str:
        return {"sse": "https", "psse": "http"}[scheme]
