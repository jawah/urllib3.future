# Copyright 2022 Akamai Technologies, Inc
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

import ssl
from collections import deque
from os import environ
from time import monotonic
from typing import Iterable, Sequence

import qh3.h3.events as h3_events
import qh3.quic.events as quic_events
from qh3.h3.connection import H3Connection, ProtocolError
from qh3.h3.exceptions import H3Error
from qh3.quic.configuration import QuicConfiguration
from qh3.quic.connection import QuicConnection, QuicConnectionError
from qh3.tls import SessionTicket

from ..._configuration import QuicTLSConfig
from ..._typing import AddressType, HeadersType
from ...events import ConnectionTerminated, DataReceived, Event
from ...events import HandshakeCompleted as _HandshakeCompleted
from ...events import HeadersReceived, StreamResetReceived
from .._protocols import HTTP3Protocol


class HTTP3ProtocolAioQuicImpl(HTTP3Protocol):
    implementation: str = "qh3"

    def __init__(
        self,
        *,
        remote_address: AddressType,
        server_name: str,
        tls_config: QuicTLSConfig,
    ) -> None:
        keylogfile_path: str | None = environ.get("SSLKEYLOGFILE", None)

        self._configuration: QuicConfiguration = QuicConfiguration(
            is_client=True,
            verify_mode=ssl.CERT_NONE if tls_config.insecure else ssl.CERT_REQUIRED,
            cafile=tls_config.cafile,
            capath=tls_config.capath,
            cadata=tls_config.cadata,
            alpn_protocols=["h3"],
            session_ticket=tls_config.session_ticket,
            server_name=server_name,
            hostname_checks_common_name=tls_config.cert_use_common_name,
            assert_fingerprint=tls_config.cert_fingerprint,
            verify_hostname=tls_config.verify_hostname,
            secrets_log_file=open(keylogfile_path, "w+") if keylogfile_path else None,  # type: ignore[arg-type]
        )

        if tls_config.certfile:
            self._configuration.load_cert_chain(
                tls_config.certfile,  # type: ignore[arg-type]
                tls_config.keyfile,  # type: ignore[arg-type]
                tls_config.keypassword,
            )

        self._configuration.load_verify_locations(tls_config.cafile)

        self._quic: QuicConnection = QuicConnection(configuration=self._configuration)
        self._connection_ids: set[bytes] = set()
        self._remote_address = remote_address
        self._event_buffer: deque[Event] = deque()
        self._http: H3Connection | None = None
        self._terminated: bool = False

    @staticmethod
    def exceptions() -> tuple[type[BaseException], ...]:
        return ProtocolError, H3Error, QuicConnectionError

    def is_available(self) -> bool:
        # TODO: check concurrent stream limit
        return not self._terminated

    def has_expired(self) -> bool:
        # TODO: check that we do not run out of stream IDs.
        return self._terminated

    @property
    def session_ticket(self) -> SessionTicket | None:
        return self._quic.tls.session_ticket if self._quic and self._quic.tls else None

    def get_available_stream_id(self) -> int:
        return self._quic.get_next_available_stream_id()

    def submit_close(self, error_code: int = 0) -> None:
        # QUIC has two different frame types for closing the connection.
        # From RFC 9000 (QUIC: A UDP-Based Multiplexed and Secure Transport):
        #
        # > An endpoint sends a CONNECTION_CLOSE frame (type=0x1c or 0x1d)
        # > to notify its peer that the connection is being closed.
        # > The CONNECTION_CLOSE frame with a type of 0x1c is used to signal errors
        # > at only the QUIC layer, or the absence of errors (with the NO_ERROR code).
        # > The CONNECTION_CLOSE frame with a type of 0x1d is used
        # > to signal an error with the application that uses QUIC.
        frame_type = 0x1D if error_code else 0x1C
        self._quic.close(error_code=error_code, frame_type=frame_type)

    def submit_headers(
        self, stream_id: int, headers: HeadersType, end_stream: bool = False
    ) -> None:
        assert self._http is not None
        self._http.send_headers(stream_id, list(headers), end_stream)

    def submit_data(
        self, stream_id: int, data: bytes, end_stream: bool = False
    ) -> None:
        assert self._http is not None
        self._http.send_data(stream_id, data, end_stream)

    def submit_stream_reset(self, stream_id: int, error_code: int = 0) -> None:
        self._quic.reset_stream(stream_id, error_code)

    def next_event(self) -> Event | None:
        if not self._event_buffer:
            return None
        return self._event_buffer.popleft()

    def has_pending_event(self) -> bool:
        return len(self._event_buffer) > 0

    @property
    def connection_ids(self) -> Sequence[bytes]:
        return list(self._connection_ids)

    def clock(self, now: float) -> None:
        timer = self._quic.get_timer()
        if timer is not None and now >= timer:
            self._quic.handle_timer(now)
            self._fetch_events()

    def get_timer(self) -> float | None:
        return self._quic.get_timer()

    def connection_lost(self) -> None:
        self._terminated = True
        self._event_buffer.append(ConnectionTerminated())

    def bytes_received(self, data: bytes) -> None:
        self._quic.receive_datagram(data, self._remote_address, now=monotonic())
        self._fetch_events()

    def bytes_to_send(self) -> bytes:
        now = monotonic()

        if self._http is None:
            self._quic.connect(self._remote_address, now=now)
            self._http = H3Connection(self._quic)

        return b"".join(
            list(map(lambda e: e[0], self._quic.datagrams_to_send(now=now)))
        )

    def _fetch_events(self) -> None:
        assert self._http is not None

        for quic_event in iter(self._quic.next_event, None):
            self._event_buffer += self._map_quic_event(quic_event)
            for h3_event in self._http.handle_event(quic_event):
                self._event_buffer += self._map_h3_event(h3_event)

        if hasattr(self._quic, "_close_event") and self._quic._close_event is not None:
            self._event_buffer += self._map_quic_event(self._quic._close_event)

    def _map_quic_event(self, quic_event: quic_events.QuicEvent) -> Iterable[Event]:
        if isinstance(quic_event, quic_events.ConnectionIdIssued):
            self._connection_ids.add(quic_event.connection_id)
        elif isinstance(quic_event, quic_events.ConnectionIdRetired):
            try:
                self._connection_ids.remove(quic_event.connection_id)
            except (
                KeyError
            ):  # it is surprising, learn more about this with aioquic maintainer.
                pass

        if isinstance(quic_event, quic_events.HandshakeCompleted):
            yield _HandshakeCompleted(quic_event.alpn_protocol)
        elif isinstance(quic_event, quic_events.ConnectionTerminated):
            self._terminated = True
            yield ConnectionTerminated(quic_event.error_code, quic_event.reason_phrase)
        elif isinstance(quic_event, quic_events.StreamReset):
            yield StreamResetReceived(quic_event.stream_id, quic_event.error_code)

    def _map_h3_event(self, h3_event: h3_events.H3Event) -> Iterable[Event]:
        if isinstance(h3_event, h3_events.HeadersReceived):
            yield HeadersReceived(
                h3_event.stream_id, h3_event.headers, h3_event.stream_ended
            )
        elif isinstance(h3_event, h3_events.DataReceived):
            yield DataReceived(h3_event.stream_id, h3_event.data, h3_event.stream_ended)

    def should_wait_remote_flow_control(
        self, stream_id: int, amt: int | None = None
    ) -> bool | None:
        return True
