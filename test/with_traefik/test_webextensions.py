from __future__ import annotations

import pytest

from urllib3 import HttpVersion, PoolManager, Timeout
from urllib3.contrib.webextensions import (
    RawExtensionFromHTTP,
    WebSocketExtensionFromHTTP,
)
from urllib3.exceptions import ReadTimeoutError

from . import TraefikTestCase


class TestWebExtensions(TraefikTestCase):
    @pytest.mark.skipif(
        WebSocketExtensionFromHTTP is None, reason="test requires wsproto"
    )
    @pytest.mark.parametrize(
        "target_protocol",
        [
            "wss",
            "ws",
        ],
    )
    def test_basic_websocket_automated(self, target_protocol: str) -> None:
        """
        This scenario verify the fundamentals around WebSocket as most
        users will do.
        """
        target_url = self.https_url if target_protocol == "wss" else self.http_url
        target_url = (
            target_url.replace("https://", "wss://")
            if target_protocol == "wss"
            else target_url.replace("http://", "ws://")
        )

        with PoolManager(resolver=self.test_resolver, ca_certs=self.ca_authority) as pm:
            resp = pm.urlopen("GET", target_url + "/websocket/echo")

            # The response ends with a "101 Switching Protocol"!
            assert resp.status == 101

            # The HTTP extension should be automatically loaded!
            assert resp.extension is not None

            # This response should not have a body, therefor don't try to read from
            # socket in there!
            assert resp.data == b""
            assert resp.read() == b""

            # the extension here should be WebSocketExtensionFromHTTP
            assert isinstance(resp.extension, WebSocketExtensionFromHTTP)

            # send two example payloads, one of type string, one of type bytes.
            resp.extension.send_payload("Hello World!")
            resp.extension.send_payload(b"Foo Bar Baz!")

            # they should be echoed in order.
            assert resp.extension.next_payload() == "Hello World!"
            assert resp.extension.next_payload() == b"Foo Bar Baz!"

            # gracefully close the sub protocol.
            resp.extension.close()

    @pytest.mark.skipif(
        WebSocketExtensionFromHTTP is None, reason="test requires wsproto"
    )
    @pytest.mark.parametrize(
        "target_protocol",
        [
            "https",
            "http",
        ],
    )
    def test_basic_websocket_manual(self, target_protocol: str) -> None:
        """
        Users shall be capable of negotiating WebSocket manually. Therefor
        urllib3-future wouldn't know it's about WebSocket and would return an
        agnostic HTTP extension (direct stream access I/O). Leaving the
        protocol part to the user capable hands!
        """
        target_url = self.https_url if target_protocol == "https" else self.http_url
        import wsproto

        with PoolManager(
            disabled_svn={HttpVersion.h2, HttpVersion.h3},
            resolver=self.test_resolver,
            ca_certs=self.ca_authority,
        ) as pm:
            protocol = wsproto.WSConnection(wsproto.connection.CLIENT)

            raw_data_to_socket = protocol.send(
                wsproto.events.Request("example.com", "/")
            )

            raw_headers = raw_data_to_socket.split(b"\r\n")[2:-2]
            request_headers: dict[str, str] = {}

            for raw_header in raw_headers:
                k, v = raw_header.decode().split(": ")
                request_headers[k.lower()] = v

            resp = pm.urlopen(
                "GET",
                target_url + "/websocket/echo",
                headers=request_headers,
            )

            # The response ends with a "101 Switching Protocol"!
            assert resp.status == 101

            # The HTTP extension should be automatically loaded!
            assert resp.extension is not None

            # This response should not have a body, therefor don't try to read from
            # socket in there!
            assert resp.data == b""
            assert resp.read() == b""

            # the extension here should be RawExtensionFromHTTP
            assert isinstance(resp.extension, RawExtensionFromHTTP)

            fake_http_response = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"

            fake_http_response += b"Sec-Websocket-Accept: "

            accept_token: str | None = resp.headers.get("Sec-Websocket-Accept")

            assert accept_token is not None

            fake_http_response += accept_token.encode() + b"\r\n\r\n"

            protocol.receive_data(fake_http_response)

            next(protocol.events())  # just remove the "Accept" event from queue!

            # send two example payloads, one of type string, one of type bytes.
            resp.extension.send_payload(
                protocol.send(wsproto.events.TextMessage("Hello World!"))
            )
            resp.extension.send_payload(
                protocol.send(wsproto.events.BytesMessage(b"Foo Bar Baz!"))
            )

            protocol.receive_data(resp.extension.next_payload())

            # they should be echoed in order.
            event_a = next(protocol.events())
            assert isinstance(event_a, wsproto.events.TextMessage)
            assert event_a.data == "Hello World!"

            try:
                event_b = next(protocol.events())
            except StopIteration:
                protocol.receive_data(resp.extension.next_payload())
                event_b = next(protocol.events())

            assert isinstance(event_b, wsproto.events.BytesMessage)
            assert event_b.data == b"Foo Bar Baz!"

            resp.extension.send_payload(
                protocol.send(wsproto.events.CloseConnection(0))
            )

            # gracefully close the sub protocol.
            resp.extension.close()

    @pytest.mark.skipif(
        WebSocketExtensionFromHTTP is None, reason="test requires wsproto"
    )
    @pytest.mark.parametrize(
        "target_protocol",
        [
            "https",
            "http",
        ],
    )
    def test_basic_websocket_using_extension_kwargs(self, target_protocol: str) -> None:
        """
        This scenario verify the fundamentals around WebSocket as most
        users will do.
        """
        target_url = self.https_url if target_protocol == "https" else self.http_url

        with PoolManager(resolver=self.test_resolver, ca_certs=self.ca_authority) as pm:
            resp = pm.urlopen(
                "GET",
                target_url + "/websocket/echo",
                extension=WebSocketExtensionFromHTTP(),
            )

            # The response ends with a "101 Switching Protocol"!
            assert resp.status == 101

            # The HTTP extension should be automatically loaded!
            assert resp.extension is not None

            # This response should not have a body, therefor don't try to read from
            # socket in there!
            assert resp.data == b""
            assert resp.read() == b""

            # the extension here should be WebSocketExtensionFromHTTP
            assert isinstance(resp.extension, WebSocketExtensionFromHTTP)

            # send two example payloads, one of type string, one of type bytes.
            resp.extension.send_payload("Hello World!")
            resp.extension.send_payload(b"Foo Bar Baz!")

            # they should be echoed in order.
            assert resp.extension.next_payload() == "Hello World!"
            assert resp.extension.next_payload() == b"Foo Bar Baz!"

            # gracefully close the sub protocol.
            resp.extension.close()

    @pytest.mark.skipif(
        WebSocketExtensionFromHTTP is None, reason="test requires wsproto"
    )
    @pytest.mark.parametrize(
        "target_protocol",
        [
            "wss",
            "ws",
        ],
    )
    def test_exception_leak_read_timeout(self, target_protocol: str) -> None:
        """Here we test both wss and ws because the low-level exception differ, we must
        check that both lead to our unified ReadTimeoutError."""
        target_url = self.https_url if target_protocol == "wss" else self.http_url
        target_url = (
            target_url.replace("https://", "wss://")
            if target_protocol == "wss"
            else target_url.replace("http://", "ws://")
        )

        with PoolManager(
            resolver=self.test_resolver,
            ca_certs=self.ca_authority,
            timeout=Timeout(read=1),
        ) as pm:
            resp = pm.urlopen("GET", target_url + "/websocket/echo")

            # The response ends with a "101 Switching Protocol"!
            assert resp.status == 101

            # The HTTP extension should be automatically loaded!
            assert resp.extension is not None

            # This response should not have a body, therefor don't try to read from
            # socket in there!
            assert resp.data == b""
            assert resp.read() == b""

            # the extension here should be WebSocketExtensionFromHTTP
            assert isinstance(resp.extension, WebSocketExtensionFromHTTP)

            with pytest.raises(ReadTimeoutError):
                resp.extension.next_payload()

            resp.extension.close()
