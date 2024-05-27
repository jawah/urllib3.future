# Copyright 2022 Akamai Technologies, Inc
# Largely rewritten in 2023 for urllib3-future
# Copyright 2024 Ahmed Tahri
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from functools import lru_cache

import h11

from ..._stream_matrix import StreamMatrix
from ..._typing import HeadersType
from ...events import ConnectionTerminated, DataReceived, Event, HeadersReceived
from .._protocols import HTTP1Protocol


@lru_cache(maxsize=64)
def capitalize_header_name(name: bytes) -> bytes:
    """
    Take a header name and capitalize it.
    >>> capitalize_header_name(b"x-hEllo-wORLD")
    'X-Hello-World'
    >>> capitalize_header_name(b"server")
    'Server'
    >>> capitalize_header_name(b"contEnt-TYPE")
    'Content-Type'
    >>> capitalize_header_name(b"content_type")
    'Content-Type'
    """
    return b"-".join(el.capitalize() for el in name.split(b"-"))


def headers_to_request(headers: HeadersType) -> h11.Event:
    method = authority = path = host = None
    regular_headers = []

    for name, value in headers:
        if name.startswith(b":"):
            if name == b":method":
                method = value
            elif name == b":scheme":
                pass
            elif name == b":authority":
                authority = value
            elif name == b":path":
                path = value
            else:
                raise ValueError("Unexpected request header: " + name.decode())
        else:
            if host is None and name == b"host":
                host = value

            # We found that many projects... actually expect the header name to be sent capitalized... hardcoded
            # within their tests. Bad news, we have to keep doing this nonsense (namely capitalize_header_name)
            regular_headers.append((capitalize_header_name(name), value))

    if authority is None:
        raise ValueError("Missing request header: :authority")

    if method == b"CONNECT":
        # CONNECT requests are a special case.
        target = authority
    else:
        target = path  # type: ignore[assignment]

    if host is None:
        regular_headers.insert(0, (b"Host", authority))
    elif host != authority:
        raise ValueError("Host header does not match :authority.")

    return h11.Request(
        method=method,  # type: ignore[arg-type]
        headers=regular_headers,
        target=target,
    )


def headers_from_response(
    response: h11.InformationalResponse | h11.Response,
) -> HeadersType:
    """
    Converts an HTTP/1.0 or HTTP/1.1 response to HTTP/2-like headers.

    Generates from pseudo (colon) headers from a response line.
    """
    return [
        (b":status", str(response.status_code).encode("ascii"))
    ] + response.headers.raw_items()


class HTTP1ProtocolHyperImpl(HTTP1Protocol):
    implementation: str = "h11"

    def __init__(self) -> None:
        self._connection: h11.Connection = h11.Connection(h11.CLIENT)
        self._data_buffer: list[bytes] = []
        self._events: StreamMatrix = StreamMatrix()
        self._terminated: bool = False
        self._switched: bool = False

        self._current_stream_id: int = 1

    @staticmethod
    def exceptions() -> tuple[type[BaseException], ...]:
        return h11.LocalProtocolError, h11.ProtocolError, h11.RemoteProtocolError

    def is_available(self) -> bool:
        return self._connection.our_state == self._connection.their_state == h11.IDLE

    def is_idle(self) -> bool:
        return self.is_available()

    def has_expired(self) -> bool:
        return self._terminated

    def get_available_stream_id(self) -> int:
        if not self.is_available():
            raise RuntimeError(
                "Cannot generate a new stream ID because the connection is not idle. "
                "HTTP/1.1 is not multiplexed and we do not support HTTP pipelining."
            )
        return self._current_stream_id

    def submit_close(self, error_code: int = 0) -> None:
        pass  # no-op

    def submit_headers(
        self, stream_id: int, headers: HeadersType, end_stream: bool = False
    ) -> None:
        if stream_id != self._current_stream_id:
            raise ValueError("Invalid stream ID.")

        self._h11_submit(headers_to_request(headers))

        if end_stream:
            self._h11_submit(h11.EndOfMessage())

    def submit_data(
        self, stream_id: int, data: bytes, end_stream: bool = False
    ) -> None:
        if stream_id != self._current_stream_id:
            raise ValueError("Invalid stream ID.")
        if self._connection.their_state == h11.SWITCHED_PROTOCOL:
            self._data_buffer.append(data)
            if end_stream:
                self._events.append(self._connection_terminated())
            return
        self._h11_submit(h11.Data(data))
        if end_stream:
            self._h11_submit(h11.EndOfMessage())

    def submit_stream_reset(self, stream_id: int, error_code: int = 0) -> None:
        # HTTP/1 cannot submit a stream (it does not have real streams).
        # But if there are no other streams, we can close the connection instead.
        self.connection_lost()

    def connection_lost(self) -> None:
        if self._connection.their_state == h11.SWITCHED_PROTOCOL:
            self._events.append(self._connection_terminated())
            return
        # This method is called when the connection is closed without an EOF.
        # But not all connections support EOF, so being here does not
        # necessarily mean that something when wrong.
        #
        # The tricky part is that HTTP/1.0 server can send responses
        # without Content-Length or Transfer-Encoding headers,
        # meaning that a response body is closed with the connection.
        # In such cases, we require a proper EOF to distinguish complete
        # messages from partial messages interrupted by network failure.
        if not self._terminated:
            self._connection.send_failed()
            self._events.append(self._connection_terminated())

    def eof_received(self) -> None:
        if self._connection.their_state == h11.SWITCHED_PROTOCOL:
            self._events.append(self._connection_terminated())
            return
        self._h11_data_received(b"")

    def bytes_received(self, data: bytes) -> None:
        if not data:
            return  # h11 treats empty data as EOF.
        if self._connection.their_state == h11.SWITCHED_PROTOCOL:
            self._events.append(DataReceived(self._current_stream_id, data))
            return
        else:
            self._h11_data_received(data)

    def bytes_to_send(self) -> bytes:
        data = b"".join(self._data_buffer)
        self._data_buffer.clear()
        self._maybe_start_next_cycle()
        return data

    def next_event(self, stream_id: int | None = None) -> Event | None:
        return self._events.popleft(stream_id=stream_id)

    def has_pending_event(self, *, stream_id: int | None = None) -> bool:
        return self._events.count(stream_id=stream_id) > 0

    def _h11_submit(self, h11_event: h11.Event) -> None:
        chunks = self._connection.send_with_data_passthrough(h11_event)
        if chunks:
            self._data_buffer += chunks

    def _h11_data_received(self, data: bytes) -> None:
        self._connection.receive_data(data)
        self._fetch_events()

    def _fetch_events(self) -> None:
        a = self._events.append
        while not self._terminated:
            try:
                h11_event = self._connection.next_event()
            except h11.RemoteProtocolError as e:
                a(self._connection_terminated(e.error_status_hint, str(e)))
                break
            if h11_event is h11.NEED_DATA or h11_event is h11.PAUSED:
                if h11.MUST_CLOSE == self._connection.their_state:
                    a(self._connection_terminated())
                else:
                    break
            elif isinstance(h11_event, (h11.Response, h11.InformationalResponse)):
                a(self._headers_from_h11_response(h11_event))
            elif isinstance(h11_event, h11.Data):
                a(self._data_from_h11(h11_event))
            elif isinstance(h11_event, h11.EndOfMessage):
                # HTTP/2 and HTTP/3 send END_STREAM flag with HEADERS and DATA frames.
                # We emulate similar behavior for HTTP/1.
                last_event: DataReceived = DataReceived(
                    self._current_stream_id, b"", self._connection.their_state != h11.MIGHT_SWITCH_PROTOCOL  # type: ignore[attr-defined]
                )
                a(last_event)
                self._maybe_start_next_cycle()
            elif isinstance(h11_event, h11.ConnectionClosed):
                a(self._connection_terminated())

    def _headers_from_h11_response(
        self, h11_event: h11.Response | h11.InformationalResponse
    ) -> Event:
        return HeadersReceived(
            self._current_stream_id, headers_from_response(h11_event)
        )

    def _data_from_h11(self, h11_event: h11.Data) -> Event:
        return DataReceived(self._current_stream_id, h11_event.data)

    def _connection_terminated(
        self, error_code: int = 0, message: str | None = None
    ) -> Event:
        self._terminated = True
        return ConnectionTerminated(error_code, message)

    def _maybe_start_next_cycle(self) -> None:
        if h11.DONE == self._connection.our_state == self._connection.their_state:
            self._connection.start_next_cycle()
            self._current_stream_id += 1
        if h11.SWITCHED_PROTOCOL == self._connection.their_state and not self._switched:
            data, closed = self._connection.trailing_data
            if data:
                self._events.append(DataReceived(self._current_stream_id, data))
            self._switched = True

    def reshelve(self, *events: Event) -> None:
        for ev in reversed(events):
            self._events.appendleft(ev)
