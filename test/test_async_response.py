from __future__ import annotations

import contextlib
import gzip
import ssl
import typing
import zlib
from base64 import b64decode
from io import BytesIO
from test import onlyBrotli, onlyZstd
from unittest import mock

import pytest

from urllib3._collections import HTTPHeaderDict
from urllib3._async.response import AsyncHTTPResponse
from urllib3.backend._async._base import AsyncLowLevelResponse
from urllib3.exceptions import (
    DecodeError,
    IncompleteRead,
    InvalidHeader,
    ProtocolError,
    SSLError,
)
from urllib3.response import (  # type: ignore[attr-defined]
    brotli,
    zstd,
)
from urllib3.util.retry import RequestHistory, Retry


def _make_async_fp(
    data: bytes,
    *,
    method: str = "GET",
    status: int = 200,
    version: int = 20,
    reason: str = "OK",
    headers: dict[str, str] | None = None,
    chunked: bool = False,
) -> AsyncLowLevelResponse:
    """Build an ``AsyncLowLevelResponse`` backed by ``data``.

    The response exercises the real async read path (``isinstance(fp,
    AsyncLowLevelResponse)`` → ``await fp.read()``) rather than falling
    back to the synchronous ``BytesIO.read()`` branch.
    """
    buffer = BytesIO(data)

    async def _body_reader(
        amt: int | None, _stream_id: int | None
    ) -> tuple[bytes, bool, HTTPHeaderDict | None]:
        chunk = buffer.read(amt) if amt else buffer.read()
        eot = len(chunk) == 0 or buffer.tell() >= len(data)
        return chunk, eot, None

    resp_headers = HTTPHeaderDict(headers or {})
    if chunked:
        resp_headers["transfer-encoding"] = "chunked"

    return AsyncLowLevelResponse(
        method=method,
        status=status,
        version=version,
        reason=reason,
        headers=resp_headers,
        body=_body_reader,
    )


# A known random (i.e, not-too-compressible) payload generated with:
#    "".join(random.choice(string.printable) for i in range(512))
#    .encode("zlib").encode("base64")
# Randomness in tests == bad, and fixing a seed may not be sufficient.
ZLIB_PAYLOAD = b64decode(
    b"""\
eJwFweuaoQAAANDfineQhiKLUiaiCzvuTEmNNlJGiL5QhnGpZ99z8luQfe1AHoMioB+QSWHQu/L+
lzd7W5CipqYmeVTBjdgSATdg4l4Z2zhikbuF+EKn69Q0DTpdmNJz8S33odfJoVEexw/l2SS9nFdi
pis7KOwXzfSqarSo9uJYgbDGrs1VNnQpT9f8zAorhYCEZronZQF9DuDFfNK3Hecc+WHLnZLQptwk
nufw8S9I43sEwxsT71BiqedHo0QeIrFE01F/4atVFXuJs2yxIOak3bvtXjUKAA6OKnQJ/nNvDGKZ
Khe5TF36JbnKVjdcL1EUNpwrWVfQpFYJ/WWm2b74qNeSZeQv5/xBhRdOmKTJFYgO96PwrHBlsnLn
a3l0LwJsloWpMbzByU5WLbRE6X5INFqjQOtIwYz5BAlhkn+kVqJvWM5vBlfrwP42ifonM5yF4ciJ
auHVks62997mNGOsM7WXNG3P98dBHPo2NhbTvHleL0BI5dus2JY81MUOnK3SGWLH8HeWPa1t5KcW
S5moAj5HexY/g/F8TctpxwsvyZp38dXeLDjSQvEQIkF7XR3YXbeZgKk3V34KGCPOAeeuQDIgyVhV
nP4HF2uWHA=="""
)


@pytest.mark.asyncio
class TestAsyncResponse:
    async def test_cache_content(self) -> None:
        r = AsyncHTTPResponse(b"foo")
        assert r._body == b"foo"
        assert await r.data == b"foo"
        assert r._body == b"foo"

    async def test_cache_content_preload_false(self) -> None:
        fp = _make_async_fp(b"foo")
        r = AsyncHTTPResponse(fp, preload_content=False)

        assert not r._body
        assert await r.data == b"foo"
        assert r._body == b"foo"  # type: ignore[comparison-overlap]
        assert await r.data == b"foo"

    async def test_default(self) -> None:
        r = AsyncHTTPResponse()
        assert await r.data is None

    async def test_none(self) -> None:
        r = AsyncHTTPResponse(None)  # type: ignore[arg-type]
        assert await r.data is None

    async def test_preload(self) -> None:
        fp = _make_async_fp(b"foo")

        r = AsyncHTTPResponse(fp, preload_content=False)
        # AsyncHTTPResponse cannot eagerly preload in __init__ (not async),
        # so we trigger the read via data.
        assert await r.data == b"foo"

    async def test_no_preload(self) -> None:
        fp = _make_async_fp(b"foo")

        r = AsyncHTTPResponse(fp, preload_content=False)

        assert await r.data == b"foo"
        assert fp.closed is True

    async def test_decode_bad_data(self) -> None:
        fp = _make_async_fp(b"\x00" * 10)
        r = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "deflate"},
            preload_content=False,
        )
        with pytest.raises(DecodeError):
            await r.read()

    async def test_reference_read(self) -> None:
        fp = _make_async_fp(b"foo")
        r = AsyncHTTPResponse(fp, preload_content=False)

        assert await r.read(0) == b""
        assert await r.read(1) == b"f"
        assert await r.read(2) == b"oo"
        assert await r.read() == b""
        assert await r.read() == b""

    async def test_decode_deflate(self) -> None:
        data = zlib.compress(b"foo")

        fp = _make_async_fp(data)
        r = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "deflate"},
            preload_content=False,
        )

        assert await r.data == b"foo"

    async def test_decode_deflate_case_insensitive(self) -> None:
        data = zlib.compress(b"foo")

        fp = _make_async_fp(data)
        r = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "DeFlAtE"},
            preload_content=False,
        )

        assert await r.data == b"foo"

    async def test_chunked_decoding_deflate(self) -> None:
        data = zlib.compress(b"foo")

        fp = _make_async_fp(data)
        r = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "deflate"},
            preload_content=False,
        )

        assert await r.read(1) == b"f"
        assert await r.read(2) == b"oo"
        assert await r.read() == b""
        assert await r.read() == b""

    async def test_chunked_decoding_deflate2(self) -> None:
        compress = zlib.compressobj(6, zlib.DEFLATED, -zlib.MAX_WBITS)
        data = compress.compress(b"foo")
        data += compress.flush()

        fp = _make_async_fp(data)
        r = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "deflate"},
            preload_content=False,
        )

        assert await r.read(1) == b"f"
        assert await r.read(2) == b"oo"
        assert await r.read() == b""
        assert await r.read() == b""

    async def test_chunked_decoding_gzip(self) -> None:
        compress = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
        data = compress.compress(b"foo")
        data += compress.flush()

        fp = _make_async_fp(data)
        r = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "gzip"},
            preload_content=False,
        )

        assert await r.read(1) == b"f"
        assert await r.read(2) == b"oo"
        assert await r.read() == b""
        assert await r.read() == b""

    async def test_decode_gzip_multi_member(self) -> None:
        compress = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
        data = compress.compress(b"foo")
        data += compress.flush()
        data = data * 3

        fp = _make_async_fp(data)
        r = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "gzip"},
            preload_content=False,
        )

        assert await r.data == b"foofoofoo"

    async def test_decode_gzip_error(self) -> None:
        fp = _make_async_fp(b"foo")
        r = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "gzip"},
            preload_content=False,
        )
        with pytest.raises(DecodeError):
            await r.read()

    async def test_decode_gzip_swallow_garbage(self) -> None:
        # When data comes from multiple calls to read(), data after
        # the first zlib error (here triggered by garbage) should be
        # ignored.
        compress = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
        data = compress.compress(b"foo")
        data += compress.flush()
        data = data * 3 + b"foo"

        fp = _make_async_fp(data)
        r = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "gzip"},
            preload_content=False,
        )
        ret = b""
        for _ in range(100):
            ret += await r.read(1)
            if r.closed:
                break

        assert ret == b"foofoofoo"

    async def test_chunked_decoding_gzip_swallow_garbage(self) -> None:
        compress = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
        data = compress.compress(b"foo")
        data += compress.flush()
        data = data * 3 + b"foo"

        fp = _make_async_fp(data)
        r = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "gzip"},
            preload_content=False,
        )

        assert await r.data == b"foofoofoo"

    @onlyBrotli()
    async def test_decode_brotli(self) -> None:
        data = brotli.compress(b"foo")

        fp = _make_async_fp(data)
        r = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "br"},
            preload_content=False,
        )
        assert await r.data == b"foo"

    @onlyBrotli()
    async def test_chunked_decoding_brotli(self) -> None:
        data = brotli.compress(b"foobarbaz")

        fp = _make_async_fp(data)
        r = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "br"},
            preload_content=False,
        )

        ret = b""
        for _ in range(100):
            ret += await r.read(1)
            if r.closed:
                break
        assert ret == b"foobarbaz"

    @onlyBrotli()
    async def test_decode_brotli_error(self) -> None:
        fp = _make_async_fp(b"foo")
        r = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "br"},
            preload_content=False,
        )
        with pytest.raises(DecodeError):
            await r.read()

    @onlyZstd()
    async def test_decode_zstd(self) -> None:
        data = zstd.compress(b"foo")

        fp = _make_async_fp(data)
        r = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "zstd"},
            preload_content=False,
        )
        assert await r.data == b"foo"

    @onlyZstd()
    async def test_decode_multiframe_zstd(self) -> None:
        data = (
            # Zstandard frame
            zstd.compress(b"foo")
            # skippable frame (must be ignored)
            + bytes.fromhex(
                "50 2A 4D 18"  # Magic_Number (little-endian)
                "07 00 00 00"  # Frame_Size (little-endian)
                "00 00 00 00 00 00 00"  # User_Data
            )
            # Zstandard frame
            + zstd.compress(b"bar")
        )

        fp = _make_async_fp(data)
        r = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "zstd"},
            preload_content=False,
        )
        assert await r.data == b"foobar"

    @onlyZstd()
    async def test_chunked_decoding_zstd(self) -> None:
        data = zstd.compress(b"foobarbaz")

        fp = _make_async_fp(data)
        r = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "zstd"},
            preload_content=False,
        )

        ret = b""
        for _ in range(100):
            ret += await r.read(1)
            if r.closed:
                break
        assert ret == b"foobarbaz"

    @onlyZstd()
    @pytest.mark.parametrize("data", [b"foo", b"x" * 100])
    async def test_decode_zstd_error(self, data: bytes) -> None:
        fp = _make_async_fp(data)
        r = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "zstd"},
            preload_content=False,
        )

        with pytest.raises(DecodeError):
            await r.read()

    @onlyZstd()
    @pytest.mark.parametrize("data", [b"foo", b"x" * 100])
    async def test_decode_zstd_incomplete(self, data: bytes) -> None:
        data = zstd.compress(data)
        fp = _make_async_fp(data[:-1])
        r = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "zstd"},
            preload_content=False,
        )

        with pytest.raises(DecodeError):
            await r.read()

    async def test_multi_decoding_deflate_deflate(self) -> None:
        data = zlib.compress(zlib.compress(b"foo"))

        fp = _make_async_fp(data)
        r = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "deflate, deflate"},
            preload_content=False,
        )

        assert await r.data == b"foo"

    async def test_multi_decoding_deflate_gzip(self) -> None:
        compress = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
        data = compress.compress(zlib.compress(b"foo"))
        data += compress.flush()

        fp = _make_async_fp(data)
        r = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "deflate, gzip"},
            preload_content=False,
        )

        assert await r.data == b"foo"

    async def test_multi_decoding_gzip_gzip(self) -> None:
        compress = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
        data = compress.compress(b"foo")
        data += compress.flush()

        compress = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
        data = compress.compress(data)
        data += compress.flush()

        fp = _make_async_fp(data)
        r = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "gzip, gzip"},
            preload_content=False,
        )

        assert await r.data == b"foo"

    async def test_read_multi_decoding_deflate_deflate(self) -> None:
        msg = b"foobarbaz" * 42
        data = zlib.compress(zlib.compress(msg))

        fp = _make_async_fp(data)
        r = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "deflate, deflate"},
            preload_content=False,
        )

        assert await r.read(3) == b"foo"
        assert await r.read(3) == b"bar"
        assert await r.read(3) == b"baz"
        assert await r.read(9) == b"foobarbaz"
        assert await r.read(9 * 3) == b"foobarbaz" * 3
        assert await r.read(9 * 37) == b"foobarbaz" * 37
        assert await r.read() == b""

    async def test_read_multi_decoding_too_many_links(self) -> None:
        fp = _make_async_fp(b"foo")

        # AsyncHTTPResponse.__init__ is not async, so the decoder
        # chain validation happens on first read(), not at construction.
        resp = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "gzip, deflate, br, zstd, gzip, deflate"},
            preload_content=False,
        )
        with pytest.raises(
            DecodeError,
            match="Too many content encodings in the chain: 6 > 5",
        ):
            await resp.read()

    async def test_body_blob(self) -> None:
        resp = AsyncHTTPResponse(b"foo")
        assert await resp.data == b"foo"
        assert resp.closed

    async def test_io(self) -> None:
        fp = _make_async_fp(b"foo")
        resp = AsyncHTTPResponse(fp, preload_content=False)

        assert not resp.closed
        assert resp.readable()
        assert not resp.writable()
        with pytest.raises(IOError):
            resp.fileno()

        await resp.close()
        assert resp.closed

        # also try when only data is present.
        resp3 = AsyncHTTPResponse("foodata")
        with pytest.raises(IOError):
            resp3.fileno()

        resp3._fp = 2
        # A corner case where _fp is present but doesn't have `closed`,
        # `isclosed`, or `fileno`.  Unlikely, but possible.
        assert resp3.closed
        with pytest.raises(IOError):
            resp3.fileno()

    async def test_read_with_illegal_mix_decode_toggle(self) -> None:
        data = zlib.compress(b"foo")

        fp = _make_async_fp(data)

        resp = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "deflate"},
            preload_content=False,
        )

        assert await resp.read(1) == b"f"

        with pytest.raises(
            RuntimeError,
            match=(
                r"Calling read\(decode_content=False\) is not supported after "
                r"read\(decode_content=True\) was called"
            ),
        ):
            await resp.read(1, decode_content=False)

        with pytest.raises(
            RuntimeError,
            match=(
                r"Calling read\(decode_content=False\) is not supported after "
                r"read\(decode_content=True\) was called"
            ),
        ):
            await resp.read(decode_content=False)

    async def test_read_with_mix_decode_toggle(self) -> None:
        data = zlib.compress(b"foo")

        fp = _make_async_fp(data)

        resp = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "deflate"},
            preload_content=False,
        )
        assert await resp.read(2, decode_content=False) is not None
        assert await resp.read(1, decode_content=True) == b"f"

    async def test_streaming(self) -> None:
        fp = _make_async_fp(b"foo")
        resp = AsyncHTTPResponse(fp, preload_content=False)
        stream = resp.stream(2, decode_content=False)

        assert await stream.__anext__() == b"fo"
        assert await stream.__anext__() == b"o"
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()

    async def test_streaming_tell(self) -> None:
        fp = _make_async_fp(b"foo")
        resp = AsyncHTTPResponse(fp, preload_content=False)
        stream = resp.stream(2, decode_content=False)

        position = 0

        position += len(await stream.__anext__())
        assert 2 == position
        assert position == resp.tell()

        position += len(await stream.__anext__())
        assert 3 == position
        assert position == resp.tell()

        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()

    async def test_gzipped_streaming(self) -> None:
        compress = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
        data = compress.compress(b"foo")
        data += compress.flush()

        fp = _make_async_fp(data)
        resp = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "gzip"},
            preload_content=False,
        )
        stream = resp.stream(2)

        assert await stream.__anext__() == b"fo"
        assert await stream.__anext__() == b"o"
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()

    async def test_gzipped_streaming_tell(self) -> None:
        compress = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
        uncompressed_data = b"foo"
        data = compress.compress(uncompressed_data)
        data += compress.flush()

        fp = _make_async_fp(data)
        resp = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "gzip"},
            preload_content=False,
        )
        stream = resp.stream()

        # Read everything
        payload = await stream.__anext__()
        assert payload == uncompressed_data

        assert len(data) == resp.tell()

        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()

    async def test_deflate_streaming_tell_intermediate_point(
        self,
    ) -> None:
        # Ensure that ``tell()`` returns the correct number of bytes when
        # part-way through streaming compressed content.
        NUMBER_OF_READS = 10
        PART_SIZE = 64

        class MockCompressedDataReading(BytesIO):
            """
            A BytesIO-like reader returning ``payload`` in
            ``NUMBER_OF_READS`` calls to ``read``.
            """

            def __init__(self, payload: bytes, payload_part_size: int) -> None:
                self.payloads = [
                    payload[i * payload_part_size : (i + 1) * payload_part_size]
                    for i in range(NUMBER_OF_READS + 1)
                ]

                assert b"".join(self.payloads) == payload

            def read(self, _: int) -> bytes:  # type: ignore[override]
                # Amount is unused.
                if len(self.payloads) > 0:
                    return self.payloads.pop(0)
                return b""

        uncompressed_data = zlib.decompress(ZLIB_PAYLOAD)

        payload_part_size = len(ZLIB_PAYLOAD) // NUMBER_OF_READS
        # This test uses a sync BytesIO mock because it tests the
        # decompression/streaming logic rather than the async I/O path.
        fp = MockCompressedDataReading(ZLIB_PAYLOAD, payload_part_size)
        resp = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "deflate"},
            preload_content=False,
        )
        stream = resp.stream(PART_SIZE)

        parts_positions = []
        async for part in stream:
            parts_positions.append((part, resp.tell()))
        end_of_stream = resp.tell()

        parts, positions = zip(*parts_positions)

        # Check that the payload is equal to the uncompressed data
        payload = b"".join(parts)
        assert uncompressed_data == payload

        # Check that the positions in the stream are correct
        expected = (92, 184, 230, 276, 322, 368, 414, 460)
        assert expected == positions

        # Check that the end of the stream is in the correct place
        assert len(ZLIB_PAYLOAD) == end_of_stream

        # Check that all parts have expected length
        expected_last_part_size = len(uncompressed_data) % PART_SIZE
        whole_parts = len(uncompressed_data) // PART_SIZE
        if expected_last_part_size == 0:
            expected_lengths = [PART_SIZE] * whole_parts
        else:
            expected_lengths = [PART_SIZE] * whole_parts + [expected_last_part_size]
        assert expected_lengths == [len(part) for part in parts]

    async def test_deflate_streaming(self) -> None:
        data = zlib.compress(b"foo")

        fp = _make_async_fp(data)
        resp = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "deflate"},
            preload_content=False,
        )
        stream = resp.stream(2)

        assert await stream.__anext__() == b"fo"
        assert await stream.__anext__() == b"o"
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()

    async def test_deflate2_streaming(self) -> None:
        compress = zlib.compressobj(6, zlib.DEFLATED, -zlib.MAX_WBITS)
        data = compress.compress(b"foo")
        data += compress.flush()

        fp = _make_async_fp(data)
        resp = AsyncHTTPResponse(
            fp,
            headers={"content-encoding": "deflate"},
            preload_content=False,
        )
        stream = resp.stream(2)

        assert await stream.__anext__() == b"fo"
        assert await stream.__anext__() == b"o"
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()

    async def test_empty_stream(self) -> None:
        fp = _make_async_fp(b"")
        resp = AsyncHTTPResponse(fp, preload_content=False)
        stream = resp.stream(2, decode_content=False)

        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()

    async def test_length_no_header(self) -> None:
        fp = _make_async_fp(b"12345")
        resp = AsyncHTTPResponse(fp, preload_content=False)
        assert resp.length_remaining is None

    # GHSA-mf9v-mfxr-j63j: ensure decoders honor `max_length` and that
    # streaming reads do not trigger full decompression of the response.
    _ghsa_mf9v_compressors: list[
        tuple[str, tuple[str, typing.Callable[[bytes], bytes]] | None]
    ] = [
        ("deflate1", ("deflate", zlib.compress)),
        (
            "deflate2",
            (
                "deflate",
                lambda data: (
                    zlib.compressobj(6, zlib.DEFLATED, -zlib.MAX_WBITS).compress(data)
                    + zlib.compressobj(6, zlib.DEFLATED, -zlib.MAX_WBITS).flush()
                ),
            ),
        ),
        ("gzip", ("gzip", gzip.compress)),
    ]
    if brotli is not None:
        _ghsa_mf9v_compressors.append(("brotli", ("br", brotli.compress)))
    else:
        _ghsa_mf9v_compressors.append(("brotli", None))
    if zstd is not None:
        _ghsa_mf9v_compressors.append(
            (
                "zstd",
                (
                    "zstd",
                    lambda data: (
                        zstd.ZstdCompressor().compress(data)
                        if hasattr(zstd, "ZstdCompressor")
                        else zstd.compress(data)
                    ),
                ),
            )
        )
    else:
        _ghsa_mf9v_compressors.append(("zstd", None))

    @pytest.mark.parametrize("read_method", ("read", "read1", "stream"))
    @pytest.mark.parametrize(
        "data",
        [d[1] for d in _ghsa_mf9v_compressors],
        ids=[d[0] for d in _ghsa_mf9v_compressors],
    )
    @pytest.mark.limit_memory("12 MB", current_thread_only=True)
    async def test_memory_usage_decode_with_max_length(
        self,
        request: pytest.FixtureRequest,
        read_method: str,
        data: tuple[str, typing.Callable[[bytes], bytes]] | None,
    ) -> None:
        if data is None:
            pytest.skip(f"Proper {request.node.callspec.id} decoder is not available")
        name, compress_func = data
        if name == "br":
            # Older Brotli/brotlicffi releases (notably brotlicffi 1.x and
            # Brotli < 1.2.0) silently ignore ``output_buffer_limit`` and
            # decompress the entire payload, which defeats the bomb
            # protection this test asserts on. Probe support and skip when
            # it's missing.
            from urllib3.response import BrotliDecoder

            try:
                BrotliDecoder()._decompress(b"", output_buffer_limit=1)
            except TypeError:
                pytest.skip(
                    "installed Brotli library does not support "
                    "output_buffer_limit; bomb-prevention requires "
                    "Brotli >= 1.2.0 (and not brotlicffi)"
                )
            except Exception:
                # Any decoder-side error other than TypeError means the
                # parameter was accepted; that is enough for our probe.
                pass
        if name == "zstd":
            from urllib3.response import _zstd_native

            if not _zstd_native:
                pytest.skip(
                    "third-party `zstandard` decompressobj does not support "
                    "max_length; bomb-prevention requires stdlib `compression.zstd`"
                )
        original = b"A" * (50 * 2**20)  # 50 MiB
        compressed = compress_func(original)
        limit = 1024 * 1024  # 1 MiB
        fp = _make_async_fp(compressed, headers={"content-encoding": name})
        r = AsyncHTTPResponse(
            fp, headers={"content-encoding": name}, preload_content=False
        )
        if read_method == "stream":
            chunk = await r.stream(amt=limit, decode_content=True).__anext__()
        else:
            chunk = await getattr(r, read_method)(amt=limit, decode_content=True)
        assert chunk
        if name != "br" or (brotli is not None and brotli.__name__ == "brotlicffi"):
            assert len(r._decoded_buffer) < len(original) // 2

    @pytest.mark.parametrize(
        "data",
        [d[1] for d in _ghsa_mf9v_compressors],
        ids=[d[0] for d in _ghsa_mf9v_compressors],
    )
    async def test_drain_conn_skips_decompression(
        self,
        request: pytest.FixtureRequest,
        data: tuple[str, typing.Callable[[bytes], bytes]] | None,
    ) -> None:
        if data is None:
            pytest.skip(f"Proper {request.node.callspec.id} decoder is not available")
        name, compress_func = data
        original = b"B" * (10 * 2**20)
        compressed = compress_func(original)
        fp = _make_async_fp(compressed, headers={"content-encoding": name})
        r = AsyncHTTPResponse(
            fp, headers={"content-encoding": name}, preload_content=False
        )
        first = await r.read(1024, decode_content=True)
        assert first
        assert r._has_decoded_content is True
        await r.drain_conn()
        assert r._decoder is None
        assert len(r._decoded_buffer) == 0

    async def test_length_w_valid_header(self) -> None:
        headers = {"content-length": "5"}
        fp = _make_async_fp(b"12345")

        resp = AsyncHTTPResponse(fp, headers=headers, preload_content=False)
        assert resp.length_remaining == 5

    async def test_length_w_bad_header(self) -> None:
        garbage = {"content-length": "foo"}
        fp = _make_async_fp(b"12345")

        resp = AsyncHTTPResponse(fp, headers=garbage, preload_content=False)
        assert resp.length_remaining is None

        garbage["content-length"] = "-10"
        fp2 = _make_async_fp(b"12345")
        resp = AsyncHTTPResponse(fp2, headers=garbage, preload_content=False)
        assert resp.length_remaining is None

    async def test_length_when_chunked(self) -> None:
        # This is expressly forbidden in RFC 7230 sec 3.3.2
        # We fall back to chunked in this case and try to
        # handle response ignoring content length.
        headers = {
            "content-length": "5",
            "transfer-encoding": "chunked",
        }
        fp = _make_async_fp(b"12345")

        resp = AsyncHTTPResponse(fp, headers=headers, preload_content=False)
        assert resp.length_remaining is None

    async def test_length_with_multiple_content_lengths(self) -> None:
        headers = {"content-length": "5, 5, 5"}
        garbage = {"content-length": "5, 42"}
        fp = _make_async_fp(b"abcde")

        resp = AsyncHTTPResponse(fp, headers=headers, preload_content=False)
        assert resp.length_remaining == 5

        fp2 = _make_async_fp(b"abcde")
        with pytest.raises(InvalidHeader):
            AsyncHTTPResponse(fp2, headers=garbage, preload_content=False)

    async def test_length_after_read(self) -> None:
        headers = {"content-length": "5"}

        # Test no defined length
        fp = _make_async_fp(b"12345")
        resp = AsyncHTTPResponse(fp, preload_content=False)
        await resp.read()
        assert resp.length_remaining is None

        # Test our update from content-length
        fp = _make_async_fp(b"12345")
        resp = AsyncHTTPResponse(fp, headers=headers, preload_content=False)
        await resp.read()
        assert resp.length_remaining == 0

        # Test partial read
        fp = _make_async_fp(b"12345")
        resp = AsyncHTTPResponse(fp, headers=headers, preload_content=False)
        stream = resp.stream(2)
        await stream.__anext__()
        assert resp.length_remaining == 3

    async def test_mock_httpresponse_stream(self) -> None:
        # Mock out an HTTP Request that does enough to make it through
        # urllib3's read() and close() calls, and also exhausts an
        # underlying file object.
        class MockHTTPRequest:
            def __init__(self) -> None:
                self.fp: BytesIO | None = None

            def read(self, amt: int) -> bytes:
                assert self.fp is not None
                data = self.fp.read(amt)
                if not data:
                    self.fp = None

                return data

            def close(self) -> None:
                self.fp = None

        bio = BytesIO(b"foo")
        fp = MockHTTPRequest()
        fp.fp = bio
        resp = AsyncHTTPResponse(
            fp,  # type: ignore[arg-type]
            preload_content=False,
        )
        stream = resp.stream(2)

        assert await stream.__anext__() == b"fo"
        assert await stream.__anext__() == b"o"
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()

    async def test_buggy_incomplete_read(self) -> None:
        content_length = 1337
        fp = _make_async_fp(b"")
        resp = AsyncHTTPResponse(
            fp,
            headers={"content-length": str(content_length)},
            preload_content=False,
            enforce_content_length=True,
        )
        with pytest.raises(ProtocolError) as ctx:
            await resp.read(3)

        assert isinstance(ctx.value, IncompleteRead)
        assert ctx.value.partial == 0
        assert ctx.value.expected == content_length

    async def test_chunked_head_response(self) -> None:
        async def mock_sock(
            amt: int | None, stream_id: int | None
        ) -> tuple[bytes, bool, HTTPHeaderDict | None]:
            return b"", True, None

        r = AsyncLowLevelResponse("HEAD", 200, 11, "OK", HTTPHeaderDict(), mock_sock)
        resp = AsyncHTTPResponse(
            r,
            preload_content=False,
            headers={"transfer-encoding": "chunked"},
            original_response=r,
        )
        assert resp.chunked is True

        setattr(resp, "release_conn", mock.Mock())
        async for _ in resp.stream():
            continue
        resp.release_conn.assert_called_once_with()  # type: ignore[attr-defined]

    async def test_get_case_insensitive_headers(self) -> None:
        headers = {"host": "example.com"}
        r = AsyncHTTPResponse(headers=headers)
        assert r.headers.get("host") == "example.com"
        assert r.headers.get("Host") == "example.com"

    async def test_retries(self) -> None:
        fp = _make_async_fp(b"")
        resp = AsyncHTTPResponse(fp)
        assert resp.retries is None
        retry = Retry()
        fp2 = _make_async_fp(b"")
        resp = AsyncHTTPResponse(fp2, retries=retry)
        assert resp.retries == retry

    async def test_geturl(self) -> None:
        fp = _make_async_fp(b"")
        request_url = "https://example.com"
        resp = AsyncHTTPResponse(fp, request_url=request_url)
        assert resp.geturl() == request_url

    async def test_url(self) -> None:
        fp = _make_async_fp(b"")
        request_url = "https://example.com"
        resp = AsyncHTTPResponse(fp, request_url=request_url)
        assert resp.url == request_url
        resp.url = "https://anotherurl.com"
        assert resp.url == "https://anotherurl.com"

    async def test_geturl_retries(self) -> None:
        fp = _make_async_fp(b"")
        resp = AsyncHTTPResponse(fp, request_url="http://example.com")
        request_histories = (
            RequestHistory(
                method="GET",
                url="http://example.com",
                error=None,
                status=301,
                redirect_location="https://example.com/",
            ),
            RequestHistory(
                method="GET",
                url="https://example.com/",
                error=None,
                status=301,
                redirect_location="https://www.example.com",
            ),
        )
        retry = Retry(history=request_histories)
        fp2 = _make_async_fp(b"")
        resp = AsyncHTTPResponse(fp2, retries=retry)
        assert resp.geturl() == "https://www.example.com"

    @pytest.mark.parametrize(
        ["payload", "expected_stream"],
        [
            (b"", []),
            (b"\n", [b"\n"]),
            (b"\n\n\n", [b"\n", b"\n", b"\n"]),
            (b"abc\ndef", [b"abc\n", b"def"]),
            (
                b"Hello\nworld\n\n\n!",
                [b"Hello\n", b"world\n", b"\n", b"\n", b"!"],
            ),
        ],
    )
    async def test__aiter__(self, payload: bytes, expected_stream: list[bytes]) -> None:
        actual_stream = []
        fp = _make_async_fp(payload)
        async for chunk in AsyncHTTPResponse(fp, preload_content=False):
            actual_stream.append(chunk)

        assert actual_stream == expected_stream

    async def test__aiter__decode_content(self) -> None:
        def stream() -> typing.Generator[bytes, None, None]:
            # Set up a generator to chunk the gzipped body
            compress = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
            data = compress.compress(b"foo\nbar")
            data += compress.flush()
            for i in range(0, len(data), 2):
                yield data[i : i + 2]

        chunks = list(stream())
        idx = 0

        async def mock_sock(
            amt: int | None, stream_id: int | None
        ) -> tuple[bytes, bool, HTTPHeaderDict | None]:
            nonlocal chunks, idx
            if idx >= len(chunks):
                return b"", True, None
            d = chunks[idx]
            idx += 1
            return d, False, None

        r = AsyncLowLevelResponse("GET", 200, 11, "OK", HTTPHeaderDict(), mock_sock)

        headers = {
            "transfer-encoding": "chunked",
            "content-encoding": "gzip",
        }
        resp = AsyncHTTPResponse(r, preload_content=False, headers=headers)

        data = b""
        async for c in resp:
            data += c

        assert b"foo\nbar" == data

    async def test_non_timeout_ssl_error_on_read(self) -> None:
        mac_error = ssl.SSLError(
            "SSL routines",
            "ssl3_get_record",
            "decryption failed or bad record mac",
        )

        @contextlib.contextmanager
        def make_bad_mac_fp() -> typing.Generator[BytesIO, None, None]:
            fp = BytesIO(b"")
            with mock.patch.object(fp, "read") as fp_read:
                # mac/decryption error
                fp_read.side_effect = mac_error
                yield fp

        with make_bad_mac_fp() as fp:
            resp = AsyncHTTPResponse(fp, preload_content=False)
            with pytest.raises(SSLError) as e:
                await resp.read()
            assert e.value.args[0] == mac_error

    async def test_json(self) -> None:
        fp = _make_async_fp(b'{"key": "value"}')
        resp = AsyncHTTPResponse(fp, preload_content=False)
        assert await resp.json() == {"key": "value"}

    async def test_readinto(self) -> None:
        fp = _make_async_fp(b"foobar")
        resp = AsyncHTTPResponse(fp, preload_content=False)

        buf = bytearray(3)
        n = await resp.readinto(buf)
        assert n == 3
        assert buf == b"foo"

        n = await resp.readinto(buf)
        assert n == 3
        assert buf == b"bar"

    async def test_close(self) -> None:
        fp = _make_async_fp(b"foo")
        resp = AsyncHTTPResponse(fp, preload_content=False)
        assert not resp.closed
        await resp.close()
        assert resp.closed

    async def test_drain_conn(self) -> None:
        fp = _make_async_fp(b"foobar")
        resp = AsyncHTTPResponse(fp, preload_content=False)
        await resp.drain_conn()
        # After draining, the fp should be consumed/closed
        assert fp.closed
