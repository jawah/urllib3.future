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

from secrets import token_bytes
from typing import Iterator

import jh2.config  # type: ignore
import jh2.connection  # type: ignore
import jh2.errors  # type: ignore
import jh2.events  # type: ignore
import jh2.exceptions  # type: ignore
import jh2.settings  # type: ignore

from ..._stream_matrix import StreamMatrix
from ..._typing import HeadersType
from ...events import (
    ConnectionTerminated,
    DataReceived,
    EarlyHeadersReceived,
    Event,
    GoawayReceived,
    HandshakeCompleted,
    HeadersReceived,
    StreamResetReceived,
    StreamResetSent,
)
from .._protocols import HTTP2Protocol


class _PatchedH2Connection(jh2.connection.H2Connection):  # type: ignore[misc]
    """
    This is a performance hotfix class. We internally, already keep
    track of the open stream count.
    """

    def __init__(
        self,
        config: jh2.config.H2Configuration | None = None,
        observable_impl: HTTP2ProtocolHyperImpl | None = None,
    ) -> None:
        super().__init__(config=config)
        # by default CONNECT is disabled
        # we need it to support natively WebSocket over HTTP/2 for example.
        self.local_settings = jh2.settings.Settings(
            client=True,
            initial_values={
                jh2.settings.SettingCodes.MAX_CONCURRENT_STREAMS: 100,
                jh2.settings.SettingCodes.MAX_HEADER_LIST_SIZE: self.DEFAULT_MAX_HEADER_LIST_SIZE,
                jh2.settings.SettingCodes.ENABLE_CONNECT_PROTOCOL: 1,
            },
        )
        self._observable_impl = observable_impl

    def _open_streams(self, *args, **kwargs) -> int:  # type: ignore[no-untyped-def]
        if self._observable_impl is not None:
            return self._observable_impl._open_stream_count
        return super()._open_streams(*args, **kwargs)  # type: ignore[no-any-return]

    def _receive_goaway_frame(self, frame):  # type: ignore[no-untyped-def]
        """
        Receive a GOAWAY frame on the connection.
        We purposely override this method to work around a known bug of jh2.
        """
        events = self.state_machine.process_input(
            jh2.connection.ConnectionInputs.RECV_GOAWAY
        )

        err_code = jh2.errors._error_code_from_int(frame.error_code)

        # GOAWAY allows an
        # endpoint to gracefully stop accepting new streams while still
        # finishing processing of previously established streams.
        # see https://tools.ietf.org/html/rfc7540#section-6.8
        # hyper/h2 does not allow such a thing for now. let's work around this.
        if (
            err_code == 0
            and self._observable_impl is not None
            and self._observable_impl._open_stream_count > 0
        ):
            self.state_machine.state = jh2.connection.ConnectionState.CLIENT_OPEN

        # Clear the outbound data buffer: we cannot send further data now.
        self.clear_outbound_data_buffer()

        # Fire an appropriate ConnectionTerminated event.
        new_event = jh2.events.ConnectionTerminated()
        new_event.error_code = err_code
        new_event.last_stream_id = frame.last_stream_id
        new_event.additional_data = (
            frame.additional_data if frame.additional_data else None
        )
        events.append(new_event)

        return [], events


HEADER_OR_TRAILER_TYPE_SET = {
    jh2.events.ResponseReceived,
    jh2.events.TrailersReceived,
}


class HTTP2ProtocolHyperImpl(HTTP2Protocol):
    implementation: str = "h2"

    def __init__(
        self,
        *,
        validate_outbound_headers: bool = False,
        validate_inbound_headers: bool = False,
        normalize_outbound_headers: bool = False,
        normalize_inbound_headers: bool = True,
    ) -> None:
        self._connection: jh2.connection.H2Connection = _PatchedH2Connection(
            jh2.config.H2Configuration(
                client_side=True,
                validate_outbound_headers=validate_outbound_headers,
                normalize_outbound_headers=normalize_outbound_headers,
                validate_inbound_headers=validate_inbound_headers,
                normalize_inbound_headers=normalize_inbound_headers,
            ),
            observable_impl=self,
        )
        self._open_stream_count: int = 0
        self._connection.initiate_connection()
        self._connection.increment_flow_control_window(2**24)
        self._events: StreamMatrix = StreamMatrix()
        self._terminated: bool = False
        self._goaway_to_honor: bool = False
        self._max_stream_count: int = (
            self._connection.remote_settings.max_concurrent_streams
        )
        self._max_frame_size: int = self._connection.remote_settings.max_frame_size

    def max_frame_size(self) -> int:
        return self._max_frame_size

    @staticmethod
    def exceptions() -> tuple[type[BaseException], ...]:
        return jh2.exceptions.ProtocolError, jh2.exceptions.H2Error

    def is_available(self) -> bool:
        if self._terminated:
            return False
        return self._max_stream_count > self._open_stream_count

    @property
    def max_stream_count(self) -> int:
        return self._max_stream_count

    def is_idle(self) -> bool:
        return self._terminated is False and self._open_stream_count == 0

    def has_expired(self) -> bool:
        return self._terminated or self._goaway_to_honor

    def get_available_stream_id(self) -> int:
        return self._connection.get_next_available_stream_id()  # type: ignore[no-any-return]

    def submit_close(self, error_code: int = 0) -> None:
        self._connection.close_connection(error_code)

    def submit_headers(
        self, stream_id: int, headers: HeadersType, end_stream: bool = False
    ) -> None:
        self._connection.send_headers(stream_id, headers, end_stream)
        self._connection.increment_flow_control_window(2**24, stream_id=stream_id)
        self._open_stream_count += 1

    def submit_data(
        self, stream_id: int, data: bytes, end_stream: bool = False
    ) -> None:
        self._connection.send_data(stream_id, data, end_stream)

    def submit_stream_reset(self, stream_id: int, error_code: int = 0) -> None:
        self._connection.reset_stream(stream_id, error_code)
        self._events.append(StreamResetSent(stream_id, error_code))

    def next_event(self, stream_id: int | None = None) -> Event | None:
        return self._events.popleft(stream_id=stream_id)

    def has_pending_event(
        self,
        *,
        stream_id: int | None = None,
        excl_event: tuple[type[Event], ...] | None = None,
    ) -> bool:
        return self._events.has(stream_id=stream_id, excl_event=excl_event)

    def _map_events(self, h2_events: list[jh2.events.Event]) -> Iterator[Event]:
        for e in h2_events:
            ev_type = e.__class__

            if ev_type in HEADER_OR_TRAILER_TYPE_SET:
                end_stream = e.stream_ended is not None
                if end_stream:
                    self._open_stream_count -= 1
                    stream = self._connection.streams.pop(e.stream_id)
                    self._connection._closed_streams[e.stream_id] = stream.closed_by
                yield HeadersReceived(e.stream_id, e.headers, end_stream=end_stream)
            elif ev_type is jh2.events.DataReceived:
                end_stream = e.stream_ended is not None
                if end_stream:
                    self._open_stream_count -= 1
                    stream = self._connection.streams.pop(e.stream_id)
                    self._connection._closed_streams[e.stream_id] = stream.closed_by
                self._connection.acknowledge_received_data(
                    e.flow_controlled_length, e.stream_id
                )
                yield DataReceived(e.stream_id, e.data, end_stream=end_stream)
            elif ev_type is jh2.events.InformationalResponseReceived:
                yield EarlyHeadersReceived(
                    e.stream_id,
                    e.headers,
                )
            elif ev_type is jh2.events.StreamReset:
                self._open_stream_count -= 1
                # event StreamEnded may occur before StreamReset
                if e.stream_id in self._connection.streams:
                    stream = self._connection.streams.pop(e.stream_id)
                    self._connection._closed_streams[e.stream_id] = stream.closed_by
                yield StreamResetReceived(e.stream_id, e.error_code)
            elif ev_type is jh2.events.ConnectionTerminated:
                # ConnectionTerminated from h2 means that GOAWAY was received.
                # A server can send GOAWAY for graceful shutdown, where clients
                # do not open new streams, but inflight requests can be completed.
                #
                # Saying "connection was terminated" can be confusing,
                # so we emit an event called "GoawayReceived".
                if e.error_code == 0:
                    self._goaway_to_honor = True
                    yield GoawayReceived(e.last_stream_id, e.error_code)
                else:
                    self._terminated = True
                    yield ConnectionTerminated(e.error_code, None)
            elif ev_type in {
                jh2.events.SettingsAcknowledged,
                jh2.events.RemoteSettingsChanged,
            }:
                yield HandshakeCompleted(alpn_protocol="h2")

    def connection_lost(self) -> None:
        self._connection_terminated()

    def eof_received(self) -> None:
        self._connection_terminated()

    def bytes_received(self, data: bytes) -> None:
        if not data:
            return

        try:
            h2_events = self._connection.receive_data(data)
        except jh2.exceptions.ProtocolError as e:
            self._connection_terminated(e.error_code, str(e))
        else:
            self._events.extend(self._map_events(h2_events))

        # we want to perpetually mark the connection as "saturated"
        if self._goaway_to_honor:
            self._max_stream_count = self._open_stream_count

        if self._connection.remote_settings.has_update:
            if not self._goaway_to_honor:
                self._max_stream_count = (
                    self._connection.remote_settings.max_concurrent_streams
                )
            self._max_frame_size = self._connection.remote_settings.max_frame_size

    def bytes_to_send(self) -> bytes:
        return self._connection.data_to_send()  # type: ignore[no-any-return]

    def _connection_terminated(
        self, error_code: int = 0, message: str | None = None
    ) -> None:
        if self._terminated:
            return
        error_code = int(error_code)  # Convert h2 IntEnum to an actual int
        self._terminated = True
        self._events.append(ConnectionTerminated(error_code, message))

    def should_wait_remote_flow_control(
        self, stream_id: int, amt: int | None = None
    ) -> bool | None:
        flow_remaining_bytes: int = self._connection.local_flow_control_window(
            stream_id
        )

        if amt is None:
            return flow_remaining_bytes == 0

        return amt > flow_remaining_bytes

    def reshelve(self, *events: Event) -> None:
        for ev in reversed(events):
            self._events.appendleft(ev)

    def ping(self) -> None:
        self._connection.ping(token_bytes(8))
