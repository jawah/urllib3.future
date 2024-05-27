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

import datetime
import ssl
import typing
from collections import deque
from os import environ
from time import time as monotonic
from typing import Any, Iterable, Sequence

if typing.TYPE_CHECKING:
    from typing_extensions import Literal

from qh3 import (
    CipherSuite,
    H3Connection,
    H3Error,
    ProtocolError,
    QuicConfiguration,
    QuicConnection,
    QuicConnectionError,
    QuicFileLogger,
    SessionTicket,
    h3_events,
    quic_events,
)

from ..._configuration import QuicTLSConfig
from ..._stream_matrix import StreamMatrix
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
        qlogdir_path: str | None = environ.get("QUICLOGDIR", None)

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
            secrets_log_file=open(keylogfile_path, "w") if keylogfile_path else None,  # type: ignore[arg-type]
            quic_logger=QuicFileLogger(qlogdir_path) if qlogdir_path else None,
            idle_timeout=300.0,
        )

        if tls_config.ciphers:
            available_ciphers = {c.name: c for c in CipherSuite}
            chosen_ciphers: list[CipherSuite] = []

            for cipher in tls_config.ciphers:
                if "name" in cipher and isinstance(cipher["name"], str):
                    chosen_ciphers.append(
                        available_ciphers[cipher["name"].replace("TLS_", "")]
                    )

            if len(chosen_ciphers) == 0:
                raise ValueError(
                    f"Unable to find a compatible cipher in '{tls_config.ciphers}' to establish a QUIC connection. "
                    f"QUIC support one of '{['TLS_' + e for e in available_ciphers.keys()]}' only."
                )

            self._configuration.cipher_suites = chosen_ciphers

        if tls_config.certfile:
            self._configuration.load_cert_chain(
                tls_config.certfile,
                tls_config.keyfile,
                tls_config.keypassword,
            )

        self._quic: QuicConnection = QuicConnection(configuration=self._configuration)
        self._connection_ids: set[bytes] = set()
        self._remote_address = remote_address
        self._events: StreamMatrix = StreamMatrix()
        self._packets: deque[bytes] = deque()
        self._http: H3Connection | None = None
        self._terminated: bool = False
        self._data_in_flight: bool = False
        self._open_stream_count: int = 0

    @staticmethod
    def exceptions() -> tuple[type[BaseException], ...]:
        return ProtocolError, H3Error, QuicConnectionError, AssertionError

    def is_available(self) -> bool:
        max_stream_bidi = 128  # todo: find a way to adapt dynamically to self._quic.max_concurrent_bidi_streams
        return (
            self._terminated is False
            and max_stream_bidi > self._quic.open_outbound_streams
        )

    def is_idle(self) -> bool:
        return self._terminated is False and self._open_stream_count == 0

    def has_expired(self) -> bool:
        self._quic.handle_timer(monotonic())
        if hasattr(self._quic, "_close_event") and self._quic._close_event is not None:
            self._events.extend(self._map_quic_event(self._quic._close_event))
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
        self._open_stream_count += 1
        self._http.send_headers(stream_id, list(headers), end_stream)

    def submit_data(
        self, stream_id: int, data: bytes, end_stream: bool = False
    ) -> None:
        assert self._http is not None
        self._http.send_data(stream_id, data, end_stream)
        if end_stream is False:
            self._data_in_flight = True

    def submit_stream_reset(self, stream_id: int, error_code: int = 0) -> None:
        self._quic.reset_stream(stream_id, error_code)

    def next_event(self, stream_id: int | None = None) -> Event | None:
        return self._events.popleft(stream_id=stream_id)

    def has_pending_event(self, *, stream_id: int | None = None) -> bool:
        return self._events.count(stream_id=stream_id) > 0

    @property
    def connection_ids(self) -> Sequence[bytes]:
        return list(self._connection_ids)

    def connection_lost(self) -> None:
        self._terminated = True
        self._events.append(ConnectionTerminated())

    def bytes_received(self, data: bytes) -> None:
        self._quic.receive_datagram(data, self._remote_address, now=monotonic())
        self._fetch_events()

        if self._data_in_flight:
            self._data_in_flight = False

    def bytes_to_send(self) -> bytes:
        now = monotonic()

        if self._http is None:
            self._quic.connect(self._remote_address, now=now)
            self._http = H3Connection(self._quic)

        packets = list(map(lambda e: e[0], self._quic.datagrams_to_send(now=now)))
        self._packets.extend(packets)

        if not self._packets:
            return b""

        return self._packets.popleft()

    def _fetch_events(self) -> None:
        assert self._http is not None

        for quic_event in iter(self._quic.next_event, None):
            self._events.extend(self._map_quic_event(quic_event))
            for h3_event in self._http.handle_event(quic_event):
                self._events.extend(self._map_h3_event(h3_event))

        if hasattr(self._quic, "_close_event") and self._quic._close_event is not None:
            self._events.extend(self._map_quic_event(self._quic._close_event))

    def _map_quic_event(self, quic_event: quic_events.QuicEvent) -> Iterable[Event]:
        if isinstance(quic_event, quic_events.HandshakeCompleted):
            yield _HandshakeCompleted(quic_event.alpn_protocol)
        elif isinstance(quic_event, quic_events.ConnectionTerminated):
            self._terminated = True
            yield ConnectionTerminated(quic_event.error_code, quic_event.reason_phrase)
        elif isinstance(quic_event, quic_events.StreamReset):
            self._open_stream_count -= 1
            yield StreamResetReceived(quic_event.stream_id, quic_event.error_code)

    def _map_h3_event(self, h3_event: h3_events.H3Event) -> Iterable[Event]:
        if isinstance(h3_event, h3_events.HeadersReceived):
            if h3_event.stream_ended:
                self._open_stream_count -= 1
            yield HeadersReceived(
                h3_event.stream_id, h3_event.headers, h3_event.stream_ended
            )
        elif isinstance(h3_event, h3_events.DataReceived):
            if h3_event.stream_ended:
                self._open_stream_count -= 1
            yield DataReceived(h3_event.stream_id, h3_event.data, h3_event.stream_ended)

    def should_wait_remote_flow_control(
        self, stream_id: int, amt: int | None = None
    ) -> bool | None:
        return self._data_in_flight

    @typing.overload
    def getissuercert(self, *, binary_form: Literal[True]) -> bytes | None:
        ...

    @typing.overload
    def getissuercert(
        self, *, binary_form: Literal[False] = ...
    ) -> dict[str, Any] | None:
        ...

    def getissuercert(
        self, *, binary_form: bool = False
    ) -> bytes | dict[str, typing.Any] | None:
        x509_certificate = self._quic.get_peercert()

        if x509_certificate is None:
            raise ValueError("TLS handshake has not been done yet")

        if not self._quic.get_issuercerts():
            return None

        x509_certificate = self._quic.get_issuercerts()[0]

        if binary_form:
            return x509_certificate.public_bytes()

        datetime.datetime.fromtimestamp(
            x509_certificate.not_valid_before, tz=datetime.timezone.utc
        )

        issuer_info = {
            "version": x509_certificate.version + 1,
            "serialNumber": x509_certificate.serial_number.upper(),
            "subject": [],
            "issuer": [],
            "notBefore": datetime.datetime.fromtimestamp(
                x509_certificate.not_valid_before, tz=datetime.timezone.utc
            ).strftime("%b %d %H:%M:%S %Y")
            + " UTC",
            "notAfter": datetime.datetime.fromtimestamp(
                x509_certificate.not_valid_after, tz=datetime.timezone.utc
            ).strftime("%b %d %H:%M:%S %Y")
            + " UTC",
        }

        _short_name_assoc = {
            "CN": "commonName",
            "L": "localityName",
            "ST": "stateOrProvinceName",
            "O": "organizationName",
            "OU": "organizationalUnitName",
            "C": "countryName",
            "STREET": "streetAddress",
            "DC": "domainComponent",
        }

        for raw_oid, rfc4514_attribute_name, value in x509_certificate.subject:
            issuer_info["subject"].append(  # type: ignore[attr-defined]
                (
                    (
                        _short_name_assoc[rfc4514_attribute_name],
                        value.decode(),
                    ),
                )
            )

        for raw_oid, rfc4514_attribute_name, value in x509_certificate.issuer:
            issuer_info["issuer"].append(  # type: ignore[attr-defined]
                (
                    (
                        _short_name_assoc[rfc4514_attribute_name],
                        value.decode(),
                    ),
                )
            )

        return issuer_info

    @typing.overload
    def getpeercert(self, *, binary_form: Literal[True]) -> bytes:
        ...

    @typing.overload
    def getpeercert(self, *, binary_form: Literal[False] = ...) -> dict[str, Any]:
        ...

    def getpeercert(
        self, *, binary_form: bool = False
    ) -> bytes | dict[str, typing.Any]:
        x509_certificate = self._quic.get_peercert()

        if x509_certificate is None:
            raise ValueError("TLS handshake has not been done yet")

        if binary_form:
            return x509_certificate.public_bytes()

        peer_info = {
            "version": x509_certificate.version + 1,
            "serialNumber": x509_certificate.serial_number.upper(),
            "subject": [],
            "issuer": [],
            "notBefore": datetime.datetime.fromtimestamp(
                x509_certificate.not_valid_before, tz=datetime.timezone.utc
            ).strftime("%b %d %H:%M:%S %Y")
            + " UTC",
            "notAfter": datetime.datetime.fromtimestamp(
                x509_certificate.not_valid_after, tz=datetime.timezone.utc
            ).strftime("%b %d %H:%M:%S %Y")
            + " UTC",
            "subjectAltName": [],
            "OCSP": [],
            "caIssuers": [],
            "crlDistributionPoints": [],
        }

        _short_name_assoc = {
            "CN": "commonName",
            "L": "localityName",
            "ST": "stateOrProvinceName",
            "O": "organizationName",
            "OU": "organizationalUnitName",
            "C": "countryName",
            "STREET": "streetAddress",
            "DC": "domainComponent",
        }

        for raw_oid, rfc4514_attribute_name, value in x509_certificate.subject:
            peer_info["subject"].append(  # type: ignore[attr-defined]
                (
                    (
                        _short_name_assoc[rfc4514_attribute_name],
                        value.decode(),
                    ),
                )
            )

        for raw_oid, rfc4514_attribute_name, value in x509_certificate.issuer:
            peer_info["issuer"].append(  # type: ignore[attr-defined]
                (
                    (
                        _short_name_assoc[rfc4514_attribute_name],
                        value.decode(),
                    ),
                )
            )

        for alt_name in x509_certificate.get_subject_alt_names():
            decoded_alt_name = alt_name.decode()
            in_parenthesis = decoded_alt_name[
                decoded_alt_name.index("(") + 1 : decoded_alt_name.index(")")
            ]
            if decoded_alt_name.startswith("DNS"):
                peer_info["subjectAltName"].append(("DNS", in_parenthesis))  # type: ignore[attr-defined]
            else:
                from ....resolver.utils import inet4_ntoa, inet6_ntoa

                if len(in_parenthesis) == 11:
                    ip_address_decoded = inet4_ntoa(
                        bytes.fromhex(in_parenthesis.replace(":", ""))
                    )
                else:
                    ip_address_decoded = inet6_ntoa(
                        bytes.fromhex(in_parenthesis.replace(":", ""))
                    )
                peer_info["subjectAltName"].append(("IP Address", ip_address_decoded))  # type: ignore[attr-defined]

        peer_info["OCSP"] = []

        for endpoint in x509_certificate.get_ocsp_endpoints():
            decoded_endpoint = endpoint.decode()

            peer_info["OCSP"].append(decoded_endpoint[decoded_endpoint.index("(") + 1 : -1])  # type: ignore[attr-defined]

        peer_info["caIssuers"] = []

        for endpoint in x509_certificate.get_issuer_endpoints():
            decoded_endpoint = endpoint.decode()
            peer_info["caIssuers"].append(decoded_endpoint[decoded_endpoint.index("(") + 1 : -1])  # type: ignore[attr-defined]

        peer_info["crlDistributionPoints"] = []

        pop_keys = []

        for k in peer_info:
            if isinstance(peer_info[k], list):
                peer_info[k] = tuple(peer_info[k])  # type: ignore[arg-type]
                if not peer_info[k]:
                    pop_keys.append(k)

        for k in pop_keys:
            peer_info.pop(k)

        return peer_info

    def cipher(self) -> str | None:
        cipher_suite = self._quic.get_cipher()

        if cipher_suite is None:
            raise ValueError("TLS handshake has not been done yet")

        return f"TLS_{cipher_suite.name}"

    def reshelve(self, *events: Event) -> None:
        for ev in reversed(events):
            self._events.appendleft(ev)
