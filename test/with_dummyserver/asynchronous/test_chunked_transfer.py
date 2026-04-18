from __future__ import annotations

import socket

import pytest

from dummyserver.testcase import (
    SocketDummyServerTestCase,
)
from urllib3 import AsyncHTTPConnectionPool
from urllib3.util import SKIP_HEADER


@pytest.mark.asyncio
class TestAsyncChunkedTransfer(SocketDummyServerTestCase):
    def start_chunked_handler(self) -> None:
        self.buffer = b""

        def socket_handler(listener: socket.socket) -> None:
            sock = listener.accept()[0]

            while not self.buffer.endswith(b"\r\n0\r\n\r\n"):
                self.buffer += sock.recv(65536)

            sock.send(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-type: text/plain\r\n"
                b"Content-Length: 0\r\n"
                b"\r\n"
            )
            sock.close()

        self._start_server(socket_handler)

    @pytest.mark.parametrize(
        "chunks",
        [
            ["foo", "bar", "", "bazzzzzzzzzzzzzzzzzzzzzz"],
            [b"foo", b"bar", b"", b"bazzzzzzzzzzzzzzzzzzzzzz"],
        ],
    )
    async def test_chunks(self, chunks: list[bytes | str]) -> None:
        self.start_chunked_handler()
        async with AsyncHTTPConnectionPool(self.host, self.port, retries=False) as pool:
            await pool.urlopen(
                "GET",
                "/",
                body=chunks,  # type: ignore[arg-type]
                headers=dict(DNT="1"),
                chunked=True,
            )

            assert b"Transfer-Encoding" in self.buffer
            body = self.buffer.split(b"\r\n\r\n", 1)[1]
            lines = body.split(b"\r\n")
            # Empty chunks should have been skipped, as this could not be distinguished
            # from terminating the transmission
            for i, chunk in enumerate(
                [c.decode() if isinstance(c, bytes) else c for c in chunks if c]
            ):
                assert lines[i * 2] == hex(len(chunk))[2:].encode("utf-8")
                assert lines[i * 2 + 1] == chunk.encode("utf-8")

    async def _test_body(self, data: bytes | str | None) -> None:
        self.start_chunked_handler()
        async with AsyncHTTPConnectionPool(self.host, self.port, retries=False) as pool:
            await pool.urlopen("GET", "/", data, chunked=True)
            header, body = self.buffer.split(b"\r\n\r\n", 1)

            assert b"Transfer-Encoding: chunked" in header.split(b"\r\n")
            if data:
                bdata = data if isinstance(data, bytes) else data.encode("utf-8")
                assert b"\r\n" + bdata + b"\r\n" in body
                assert body.endswith(b"\r\n0\r\n\r\n")

                len_str = body.split(b"\r\n", 1)[0]
                stated_len = int(len_str, 16)
                assert stated_len == len(bdata)
            else:
                assert body == b"0\r\n\r\n"

    async def test_bytestring_body(self) -> None:
        await self._test_body(b"thisshouldbeonechunk\r\nasdf")

    async def test_unicode_body(self) -> None:
        await self._test_body("thisshouldbeonechunk\r\näöüß")

    async def test_empty_body(self) -> None:
        await self._test_body(None)

    async def test_empty_string_body(self) -> None:
        await self._test_body("")

    async def test_empty_iterable_body(self) -> None:
        await self._test_body(None)

    def _get_header_lines(self, prefix: bytes) -> list[bytes]:
        header_block = self.buffer.split(b"\r\n\r\n", 1)[0].lower()
        header_lines = header_block.split(b"\r\n")[1:]
        return [x for x in header_lines if x.startswith(prefix)]

    async def test_removes_duplicate_host_header(self) -> None:
        self.start_chunked_handler()
        chunks = [b"foo", b"bar", b"", b"bazzzzzzzzzzzzzzzzzzzzzz"]
        async with AsyncHTTPConnectionPool(self.host, self.port, retries=False) as pool:
            await pool.urlopen(
                "GET", "/", body=chunks, headers={"Host": "test.org"}, chunked=True
            )

            host_headers = self._get_header_lines(b"host")
            assert len(host_headers) == 1

    async def test_provides_default_host_header(self) -> None:
        self.start_chunked_handler()
        chunks = [b"foo", b"bar", b"", b"bazzzzzzzzzzzzzzzzzzzzzz"]
        async with AsyncHTTPConnectionPool(self.host, self.port, retries=False) as pool:
            await pool.urlopen("GET", "/", body=chunks, chunked=True)

            host_headers = self._get_header_lines(b"host")
            assert len(host_headers) == 1

    async def test_provides_default_user_agent_header(self) -> None:
        self.start_chunked_handler()
        chunks = [b"foo", b"bar", b"", b"bazzzzzzzzzzzzzzzzzzzzzz"]
        async with AsyncHTTPConnectionPool(self.host, self.port, retries=False) as pool:
            await pool.urlopen("GET", "/", body=chunks, chunked=True)

            ua_headers = self._get_header_lines(b"user-agent")
            assert len(ua_headers) == 1

    async def test_preserve_user_agent_header(self) -> None:
        self.start_chunked_handler()
        chunks = [b"foo", b"bar", b"", b"bazzzzzzzzzzzzzzzzzzzzzz"]
        async with AsyncHTTPConnectionPool(self.host, self.port, retries=False) as pool:
            await pool.urlopen(
                "GET",
                "/",
                body=chunks,
                headers={"user-Agent": "test-agent"},
                chunked=True,
            )

            ua_headers = self._get_header_lines(b"user-agent")
            # Validate that there is only one User-Agent header.
            assert len(ua_headers) == 1
            # Validate that the existing User-Agent header is the one that was
            # provided.
            assert ua_headers[0] == b"user-agent: test-agent"

    async def test_remove_user_agent_header(self) -> None:
        self.start_chunked_handler()
        chunks = [b"foo", b"bar", b"", b"bazzzzzzzzzzzzzzzzzzzzzz"]
        async with AsyncHTTPConnectionPool(self.host, self.port, retries=False) as pool:
            await pool.urlopen(
                "GET",
                "/",
                body=chunks,
                headers={"User-Agent": SKIP_HEADER},
                chunked=True,
            )

            ua_headers = self._get_header_lines(b"user-agent")
            assert len(ua_headers) == 0

    async def test_provides_default_transfer_encoding_header(self) -> None:
        self.start_chunked_handler()
        chunks = [b"foo", b"bar", b"", b"bazzzzzzzzzzzzzzzzzzzzzz"]
        async with AsyncHTTPConnectionPool(self.host, self.port, retries=False) as pool:
            await pool.urlopen("GET", "/", body=chunks, chunked=True)

            te_headers = self._get_header_lines(b"transfer-encoding")
            assert len(te_headers) == 1
