from __future__ import annotations

import typing

if typing.TYPE_CHECKING:
    from ...._async.response import AsyncHTTPResponse

from ....backend import HttpVersion
from ..sse import ServerSentEvent
from .protocol import AsyncExtensionFromHTTP


class AsyncServerSideEventExtensionFromHTTP(AsyncExtensionFromHTTP):
    def __init__(self) -> None:
        super().__init__()

        self._last_event_id: str | None = None
        self._buffer: str = ""
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

        try:
            raw_payload: str = (await self._stream.__anext__()).decode("utf-8")
        except StopAsyncIteration:
            await self._stream.aclose()
            self._stream = None
            return None

        if self._buffer:
            raw_payload = self._buffer + raw_payload
            self._buffer = ""

        kwargs: dict[str, typing.Any] = {}
        eot = False

        for line in raw_payload.splitlines():
            if not line:
                eot = True
                break
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

        if eot is False:
            self._buffer = raw_payload
            return await self.next_payload(raw=raw)  # type: ignore[call-overload,no-any-return]

        if "id" not in kwargs and self._last_event_id is not None:
            kwargs["id"] = self._last_event_id

        event = ServerSentEvent(**kwargs)

        if event.id:
            self._last_event_id = event.id

        if raw is True:
            return raw_payload

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
