from __future__ import annotations

import typing

if typing.TYPE_CHECKING:
    from ...._async.response import AsyncHTTPResponse

from wsproto import ConnectionType, WSConnection
from wsproto.events import (
    AcceptConnection,
    BytesMessage,
    CloseConnection,
    Ping,
    Pong,
    Request,
    TextMessage,
)
from wsproto.extensions import PerMessageDeflate
from wsproto.utilities import ProtocolError as WebSocketProtocolError

from ....backend import HttpVersion
from ....exceptions import ProtocolError
from .protocol import AsyncExtensionFromHTTP


class AsyncWebSocketExtensionFromHTTP(AsyncExtensionFromHTTP):
    def __init__(self) -> None:
        super().__init__()
        self._protocol = WSConnection(ConnectionType.CLIENT)
        self._request_headers: dict[str, str] | None = None
        self._remote_shutdown: bool = False

    @staticmethod
    def supported_svn() -> set[HttpVersion]:
        return {HttpVersion.h11}

    @staticmethod
    def implementation() -> str:
        return "wsproto"

    async def start(self, response: AsyncHTTPResponse) -> None:
        await super().start(response)

        fake_http_response = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"

        fake_http_response += b"Sec-Websocket-Accept: "

        accept_token: str | None = response.headers.get("Sec-Websocket-Accept")

        if accept_token is None:
            raise RuntimeError(
                "The WebSocket HTTP extension requires 'Sec-Websocket-Accept' header in the server response but was not present."
            )

        fake_http_response += accept_token.encode() + b"\r\n\r\n"

        try:
            self._protocol.receive_data(fake_http_response)
        except WebSocketProtocolError as e:
            raise ProtocolError from e  # Defensive: should never occur!

        event = next(self._protocol.events())

        if not isinstance(event, AcceptConnection):
            raise RuntimeError(
                "The WebSocket state-machine did not pass the handshake phase when expected."
            )

    def headers(self, http_version: HttpVersion) -> dict[str, str]:
        """Specific HTTP headers required (request) before the 101 status response."""
        if self._request_headers is not None:
            return self._request_headers

        try:
            raw_data_to_socket = self._protocol.send(
                Request(
                    host="example.com", target="/", extensions=(PerMessageDeflate(),)
                )
            )
        except WebSocketProtocolError as e:
            raise ProtocolError from e  # Defensive: should never occur!

        raw_headers = raw_data_to_socket.split(b"\r\n")[2:-2]
        request_headers: dict[str, str] = {}

        for raw_header in raw_headers:
            k, v = raw_header.decode().split(": ")
            request_headers[k.lower()] = v

        if http_version != HttpVersion.h11:
            del request_headers["upgrade"]
            del request_headers["connection"]
            request_headers[":protocol"] = "websocket"
            request_headers[":method"] = "CONNECT"

        self._request_headers = request_headers

        return request_headers

    async def close(self) -> None:
        """End/Notify close for sub protocol."""
        if self._dsa is not None:
            if self._police_officer is not None:
                async with self._police_officer.borrow(self._response):
                    if self._remote_shutdown is False:
                        try:
                            data_to_send: bytes = self._protocol.send(
                                CloseConnection(0)
                            )
                        except WebSocketProtocolError as e:
                            await self.close()
                            raise ProtocolError from e
                        async with self._write_error_catcher():
                            await self._dsa.sendall(data_to_send)
                    await self._dsa.close()
                    self._dsa = None
            else:
                self._dsa = None
        if self._response is not None:
            if self._police_officer is not None:
                self._police_officer.forget(self._response)
                if self._police_officer.busy:
                    self._police_officer.release()
            else:
                await self._response.close()
            self._response = None

        self._police_officer = None

    async def next_payload(self) -> str | bytes | None:
        """Unpack the next received message/payload from remote."""
        if self._dsa is None or self._response is None or self._police_officer is None:
            raise OSError("The HTTP extension is closed or uninitialized")

        async with self._police_officer.borrow(self._response):
            for event in self._protocol.events():
                if isinstance(event, TextMessage):
                    return event.data
                elif isinstance(event, BytesMessage):
                    return event.data
                elif isinstance(event, CloseConnection):
                    self._remote_shutdown = True
                    await self.close()
                    return None
                elif isinstance(event, Ping):
                    try:
                        data_to_send: bytes = self._protocol.send(event.response())
                    except WebSocketProtocolError as e:
                        await self.close()
                        raise ProtocolError from e

                    async with self._write_error_catcher():
                        await self._dsa.sendall(data_to_send)

            while True:
                async with self._read_error_catcher():
                    data, eot, _ = await self._dsa.recv_extended(None)

                try:
                    self._protocol.receive_data(data)
                except WebSocketProtocolError as e:
                    raise ProtocolError from e

                for event in self._protocol.events():
                    if isinstance(event, TextMessage):
                        return event.data
                    elif isinstance(event, BytesMessage):
                        return event.data
                    elif isinstance(event, CloseConnection):
                        self._remote_shutdown = True
                        await self.close()
                        return None
                    elif isinstance(event, Ping):
                        data_to_send = self._protocol.send(event.response())
                        async with self._write_error_catcher():
                            await self._dsa.sendall(data_to_send)
                    elif isinstance(event, Pong):
                        continue

    async def send_payload(self, buf: str | bytes) -> None:
        """Dispatch a buffer to remote."""
        if self._dsa is None or self._response is None or self._police_officer is None:
            raise OSError("The HTTP extension is closed or uninitialized")

        async with self._police_officer.borrow(self._response):
            try:
                if isinstance(buf, str):
                    data_to_send: bytes = self._protocol.send(TextMessage(buf))
                else:
                    data_to_send = self._protocol.send(BytesMessage(buf))
            except WebSocketProtocolError as e:
                raise ProtocolError from e

            async with self._write_error_catcher():
                await self._dsa.sendall(data_to_send)

    async def ping(self) -> None:
        if self._dsa is None or self._response is None or self._police_officer is None:
            raise OSError("The HTTP extension is closed or uninitialized")

        async with self._police_officer.borrow(self._response):
            try:
                data_to_send: bytes = self._protocol.send(Ping())
            except WebSocketProtocolError as e:
                raise ProtocolError from e

            async with self._write_error_catcher():
                await self._dsa.sendall(data_to_send)

    @staticmethod
    def supported_schemes() -> set[str]:
        return {"ws", "wss"}

    @staticmethod
    def scheme_to_http_scheme(scheme: str) -> str:
        return {"ws": "http", "wss": "https"}[scheme]


class AsyncWebSocketExtensionFromMultiplexedHTTP(AsyncWebSocketExtensionFromHTTP):
    """
    Plugin that support doing WebSocket over HTTP 2 and 3.
    This implement RFC8441. Beware that this isn't actually supported by much server around internet.
    """

    @staticmethod
    def implementation() -> str:
        return "rfc8441"

    @staticmethod
    def supported_svn() -> set[HttpVersion]:
        return {HttpVersion.h11, HttpVersion.h2, HttpVersion.h3}
