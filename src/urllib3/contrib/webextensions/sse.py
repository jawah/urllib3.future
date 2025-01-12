from __future__ import annotations

import json
import typing
from threading import RLock

if typing.TYPE_CHECKING:
    from ...response import HTTPResponse

from ...backend import HttpVersion
from .protocol import ExtensionFromHTTP


class ServerSentEvent:
    def __init__(
        self,
        event: str | None = None,
        data: str | None = None,
        id: str | None = None,
        retry: int | None = None,
    ) -> None:
        if not event:
            event = "message"

        if data is None:
            data = ""

        if id is None:
            id = ""

        self._event = event
        self._data = data
        self._id = id
        self._retry = retry

    @property
    def event(self) -> str:
        return self._event

    @property
    def data(self) -> str:
        return self._data

    @property
    def id(self) -> str:
        return self._id

    @property
    def retry(self) -> int | None:
        return self._retry

    def json(self) -> typing.Any:
        return json.loads(self.data)

    def __repr__(self) -> str:
        pieces = [f"event={self.event!r}"]
        if self.data != "":
            pieces.append(f"data={self.data!r}")
        if self.id != "":
            pieces.append(f"id={self.id!r}")
        if self.retry is not None:
            pieces.append(f"retry={self.retry!r}")
        return f"ServerSentEvent({', '.join(pieces)})"


class ServerSideEventExtensionFromHTTP(ExtensionFromHTTP):
    def __init__(self) -> None:
        super().__init__()

        self._last_event_id: str | None = None
        self._buffer: str = ""
        self._lock = RLock()
        self._stream: typing.Generator[bytes, None, None] | None = None

    @staticmethod
    def supported_svn() -> set[HttpVersion]:
        return {HttpVersion.h11, HttpVersion.h2, HttpVersion.h3}

    @staticmethod
    def implementation() -> str:
        return "native"

    @property
    def urlopen_kwargs(self) -> dict[str, typing.Any]:
        return {"preload_content": False}

    @property
    def closed(self) -> bool:
        return self._stream is None

    def close(self) -> None:
        if self._stream is not None and self._response is not None:
            self._stream.close()
            if (
                self._response._fp is not None
                and self._police_officer is not None
                and hasattr(self._response._fp, "abort")
            ):
                with self._police_officer.borrow(self._response):
                    self._response._fp.abort()
            self._stream = None
            self._response = None
            self._police_officer = None

    def start(self, response: HTTPResponse) -> None:
        super().start(response)

        self._stream = response.stream(-1, decode_content=True)

    def headers(self, http_version: HttpVersion) -> dict[str, str]:
        return {"accept": "text/event-stream", "cache-control": "no-store"}

    @typing.overload
    def next_payload(self, *, raw: typing.Literal[True] = True) -> str | None: ...

    @typing.overload
    def next_payload(
        self, *, raw: typing.Literal[False] = False
    ) -> ServerSentEvent | None: ...

    def next_payload(self, *, raw: bool = False) -> ServerSentEvent | str | None:
        """Unpack the next received message/payload from remote."""
        if self._response is None or self._stream is None:
            raise OSError("The HTTP extension is closed or uninitialized")
        with self._lock:
            try:
                raw_payload: str = next(self._stream).decode("utf-8")
            except StopIteration:
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
                return self.next_payload(raw=raw)  # type: ignore[call-overload,no-any-return]

            if "id" not in kwargs and self._last_event_id is not None:
                kwargs["id"] = self._last_event_id

            event = ServerSentEvent(**kwargs)

            if event.id:
                self._last_event_id = event.id

            if raw is True:
                return raw_payload

            return event

    def send_payload(self, buf: str | bytes) -> None:
        """Dispatch a buffer to remote."""
        raise NotImplementedError("SSE is only one-way. Sending is forbidden.")

    @staticmethod
    def supported_schemes() -> set[str]:
        return {"sse", "psse"}

    @staticmethod
    def scheme_to_http_scheme(scheme: str) -> str:
        return {"sse": "https", "psse": "http"}[scheme]
