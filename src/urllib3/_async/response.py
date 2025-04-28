from __future__ import annotations

import io
import json as _json
import sys
import typing
import warnings
from contextlib import asynccontextmanager
from socket import timeout as SocketTimeout

from .._collections import HTTPHeaderDict
from .._typing import _TYPE_BODY
from ..backend._async import AsyncLowLevelResponse
from ..exceptions import (
    BaseSSLError,
    HTTPError,
    IncompleteRead,
    ProtocolError,
    ReadTimeoutError,
    ResponseNotReady,
    SSLError,
)
from ..response import ContentDecoder, HTTPResponse
from ..util.response import is_fp_closed, BytesQueueBuffer
from ..util.retry import Retry
from .connection import AsyncHTTPConnection

if typing.TYPE_CHECKING:
    from email.message import Message

    from .._async.connectionpool import AsyncHTTPConnectionPool
    from ..contrib.webextensions._async import AsyncExtensionFromHTTP
    from ..util._async.traffic_police import AsyncTrafficPolice


class AsyncHTTPResponse(HTTPResponse):
    def __init__(
        self,
        body: _TYPE_BODY = "",
        headers: typing.Mapping[str, str] | typing.Mapping[bytes, bytes] | None = None,
        status: int = 0,
        version: int = 0,
        reason: str | None = None,
        preload_content: bool = True,
        decode_content: bool = True,
        original_response: AsyncLowLevelResponse | None = None,
        pool: AsyncHTTPConnectionPool | None = None,
        connection: AsyncHTTPConnection | None = None,
        msg: Message | None = None,
        retries: Retry | None = None,
        enforce_content_length: bool = True,
        request_method: str | None = None,
        request_url: str | None = None,
        auto_close: bool = True,
        police_officer: AsyncTrafficPolice[AsyncHTTPConnection] | None = None,
    ) -> None:
        if isinstance(headers, HTTPHeaderDict):
            self.headers = headers
        else:
            self.headers = HTTPHeaderDict(headers)  # type: ignore[arg-type]
        try:
            self.status = int(status)
        except ValueError:
            self.status = 0  # merely for tests, was supported due to broken httplib.
        self.version = version
        self.reason = reason
        self.decode_content = decode_content
        self._has_decoded_content = False
        self._request_url: str | None = request_url
        self._retries: Retry | None = None

        self._extension: AsyncExtensionFromHTTP | None = None  # type: ignore[assignment]

        self.retries = retries

        self.chunked = False

        if "transfer-encoding" in self.headers:
            tr_enc = self.headers.get("transfer-encoding", "").lower()
            # Don't incur the penalty of creating a list and then discarding it
            encodings = (enc.strip() for enc in tr_enc.split(","))

            if "chunked" in encodings:
                self.chunked = True

        self._decoder: ContentDecoder | None = None

        self.enforce_content_length = enforce_content_length
        self.auto_close = auto_close

        self._body = None
        self._fp: AsyncLowLevelResponse | typing.IO[typing.Any] | None = None  # type: ignore[assignment]
        self._original_response = original_response  # type: ignore[assignment]
        self._fp_bytes_read = 0

        if msg is not None:
            warnings.warn(
                "Passing msg=.. is deprecated and no-op in urllib3.future and is scheduled to be removed in a future major.",
                DeprecationWarning,
                stacklevel=2,
            )

        self.msg = msg

        if body and isinstance(body, (str, bytes)):
            self._body = body

        self._pool: AsyncHTTPConnectionPool = pool  # type: ignore[assignment]
        self._connection: AsyncHTTPConnection = connection  # type: ignore[assignment]

        if hasattr(body, "read"):
            self._fp = body  # type: ignore[assignment]

        # Are we using the chunked-style of transfer encoding?
        self.chunk_left: int | None = None

        # Determine length of response
        self._request_method: str | None = request_method
        self.length_remaining: int | None = self._init_length(self._request_method)

        # Used to return the correct amount of bytes for partial read()s
        self._decoded_buffer = BytesQueueBuffer()

        self._police_officer: AsyncTrafficPolice[AsyncHTTPConnection] | None = (
            police_officer  # type: ignore[assignment]
        )

        self._preloaded_content: bool = preload_content

        if self._police_officer is not None:
            self._police_officer.memorize(self, self._connection)
            # we can utilize a ConnectionPool without level-0 PoolManager!
            if self._police_officer.parent is not None:
                self._police_officer.parent.memorize(self, self._pool)

    async def readinto(self, b: bytearray) -> int:  # type: ignore[override]
        temp = await self.read(len(b))
        if len(temp) == 0:
            return 0
        else:
            b[: len(temp)] = temp
            return len(temp)

    @asynccontextmanager
    async def _error_catcher(self) -> typing.AsyncGenerator[None, None]:  # type: ignore[override]
        """
        Catch low-level python exceptions, instead re-raising urllib3
        variants, so that low-level exceptions are not leaked in the
        high-level api.

        On exit, release the connection back to the pool.
        """
        clean_exit = False

        try:
            try:
                yield

            except SocketTimeout as e:
                # FIXME: Ideally we'd like to include the url in the ReadTimeoutError but
                # there is yet no clean way to get at it from this context.
                raise ReadTimeoutError(self._pool, None, "Read timed out.") from e  # type: ignore[arg-type]

            except BaseSSLError as e:
                # FIXME: Is there a better way to differentiate between SSLErrors?
                if "read operation timed out" not in str(e):
                    # SSL errors related to framing/MAC get wrapped and reraised here
                    raise SSLError(e) from e

                raise ReadTimeoutError(self._pool, None, "Read timed out.") from e  # type: ignore[arg-type]

            except OSError as e:
                # This includes IncompleteRead.
                raise ProtocolError(f"Connection broken: {e!r}", e) from e

            # If no exception is thrown, we should avoid cleaning up
            # unnecessarily.
            clean_exit = True
        finally:
            # If we didn't terminate cleanly, we need to throw away our
            # connection.
            if not clean_exit:
                # The response may not be closed but we're not going to use it
                # anymore so close it now to ensure that the connection is
                # released back to the pool.
                if self._original_response:
                    self._original_response.close()

                # Closing the response may not actually be sufficient to close
                # everything, so if we have a hold of the connection close that
                # too.
                if self._connection:
                    await self._connection.close()

            # If we hold the original response but it's closed now, we should
            # return the connection back to the pool.
            if self._original_response and self._original_response.isclosed():
                self.release_conn()

    async def drain_conn(self) -> None:  # type: ignore[override]
        """
        Read and discard any remaining HTTP response data in the response connection.

        Unread data in the HTTPResponse connection blocks the connection from being released back to the pool.
        """
        try:
            await self.read()
        except (HTTPError, OSError, BaseSSLError):
            pass

    @property
    def trailers(self) -> HTTPHeaderDict | None:
        """
        Retrieve post-response (trailing headers) if any.
        This WILL return None if no HTTP Trailer Headers have been received.
        """
        if self._fp is None:
            return None

        if hasattr(self._fp, "trailers"):
            return self._fp.trailers

        return None

    @property
    def extension(self) -> AsyncExtensionFromHTTP | None:  # type: ignore[override]
        return self._extension

    async def start_extension(self, item: AsyncExtensionFromHTTP) -> None:  # type: ignore[override]
        if self._extension is not None:
            raise OSError("extension already plugged in")

        if not hasattr(self._fp, "_dsa"):
            raise ResponseNotReady()

        await item.start(self)

        self._extension = item

    async def json(self) -> typing.Any:
        """
        Parses the body of the HTTP response as JSON.

        To use a custom JSON decoder pass the result of :attr:`HTTPResponse.data` to the decoder.

        This method can raise either `UnicodeDecodeError` or `json.JSONDecodeError`.

        Read more :ref:`here <json>`.
        """
        data = (await self.data).decode("utf-8")
        return _json.loads(data)

    @property
    async def data(self) -> bytes:  # type: ignore[override]
        # For backwards-compat with earlier urllib3 0.4 and earlier.
        if self._body:
            return self._body  # type: ignore[return-value]

        if self._fp:
            return await self.read(cache_content=True)

        return None  # type: ignore[return-value]

    async def _fp_read(self, amt: int | None = None) -> bytes:  # type: ignore[override]
        """
        Read a response with the thought that reading the number of bytes
        larger than can fit in a 32-bit int at a time via SSL in some
        known cases leads to an overflow error that has to be prevented
        if `amt` or `self.length_remaining` indicate that a problem may
        happen.

        The known cases:
          * 3.8 <= CPython < 3.9.7 because of a bug
            https://github.com/urllib3/urllib3/issues/2513#issuecomment-1152559900.
          * urllib3 injected with pyOpenSSL-backed SSL-support.
          * CPython < 3.10 only when `amt` does not fit 32-bit int.
        """
        assert self._fp
        c_int_max = 2**31 - 1
        if (
            (amt and amt > c_int_max)
            or (self.length_remaining and self.length_remaining > c_int_max)
        ) and sys.version_info < (3, 10):
            buffer = io.BytesIO()
            # Besides `max_chunk_amt` being a maximum chunk size, it
            # affects memory overhead of reading a response by this
            # method in CPython.
            # `c_int_max` equal to 2 GiB - 1 byte is the actual maximum
            # chunk size that does not lead to an overflow error, but
            # 256 MiB is a compromise.
            max_chunk_amt = 2**28
            while amt is None or amt != 0:
                if amt is not None:
                    chunk_amt = min(amt, max_chunk_amt)
                    amt -= chunk_amt
                else:
                    chunk_amt = max_chunk_amt
                try:
                    if isinstance(self._fp, AsyncLowLevelResponse):
                        data = await self._fp.read(chunk_amt)
                    else:
                        data = self._fp.read(chunk_amt)  # type: ignore[attr-defined]
                except ValueError:  # Defensive: overly protective
                    break  # Defensive: can also be an indicator that read ended, should not happen.
                if not data:
                    break
                buffer.write(data)
                del data  # to reduce peak memory usage by `max_chunk_amt`.
            return buffer.getvalue()
        else:
            # StringIO doesn't like amt=None
            if isinstance(self._fp, AsyncLowLevelResponse):
                return await self._fp.read(amt)
            return self._fp.read(amt) if amt is not None else self._fp.read()  # type: ignore[no-any-return]

    async def _raw_read(  # type: ignore[override]
        self,
        amt: int | None = None,
    ) -> bytes:
        """
        Reads `amt` of bytes from the socket.
        """
        if self._fp is None:
            return None  # type: ignore[return-value]

        fp_closed = getattr(self._fp, "closed", False)

        async with self._error_catcher():
            data = (await self._fp_read(amt)) if not fp_closed else b""

            # Mocking library often use io.BytesIO
            # which does not auto-close when reading data
            # with amt=None.
            is_foreign_fp_unclosed = (
                amt is None and getattr(self._fp, "closed", False) is False
            )

            if (amt is not None and amt != 0 and not data) or is_foreign_fp_unclosed:
                if is_foreign_fp_unclosed:
                    self._fp_bytes_read += len(data)
                    if self.length_remaining is not None:
                        self.length_remaining -= len(data)

                # Platform-specific: Buggy versions of Python.
                # Close the connection when no data is returned
                #
                # This is redundant to what httplib/http.client _should_
                # already do.  However, versions of python released before
                # December 15, 2012 (http://bugs.python.org/issue16298) do
                # not properly close the connection in all cases. There is
                # no harm in redundantly calling close.
                self._fp.close()
                if (
                    self.enforce_content_length
                    and self.length_remaining is not None
                    and self.length_remaining != 0
                ):
                    # This is an edge case that httplib failed to cover due
                    # to concerns of backward compatibility. We're
                    # addressing it here to make sure IncompleteRead is
                    # raised during streaming, so all calls with incorrect
                    # Content-Length are caught.
                    raise IncompleteRead(self._fp_bytes_read, self.length_remaining)

        if data and not is_foreign_fp_unclosed:
            self._fp_bytes_read += len(data)
            if self.length_remaining is not None:
                self.length_remaining -= len(data)

        return data

    async def read1(  # type: ignore[override]
        self,
        amt: int | None = None,
        decode_content: bool | None = None,
    ) -> bytes:
        """
        Similar to ``http.client.HTTPResponse.read1`` and documented
        in :meth:`io.BufferedReader.read1`, but with an additional parameter:
        ``decode_content``.

        :param amt:
            How much of the content to read.

        :param decode_content:
            If True, will attempt to decode the body based on the
            'content-encoding' header.
        """

        data = await self.read(
            amt=amt or -1,
            decode_content=decode_content,
        )

        if amt is not None and len(data) > amt:
            self._decoded_buffer.put(data)
            return self._decoded_buffer.get(amt)

        return data

    async def read(  # type: ignore[override]
        self,
        amt: int | None = None,
        decode_content: bool | None = None,
        cache_content: bool = False,
    ) -> bytes:
        try:
            self._init_decoder()
            if decode_content is None:
                decode_content = self.decode_content

            if amt is not None:
                cache_content = False

                if amt < 0 and len(self._decoded_buffer):
                    return self._decoded_buffer.get(len(self._decoded_buffer))

                if 0 < amt <= len(self._decoded_buffer):
                    return self._decoded_buffer.get(amt)

            if self._police_officer is not None:
                async with self._police_officer.borrow(self):
                    data = await self._raw_read(amt)
            else:
                data = await self._raw_read(amt)

            if amt and amt < 0:
                amt = len(data)

            flush_decoder = False
            if amt is None:
                flush_decoder = True
            elif amt != 0 and not data:
                flush_decoder = True

            if not data and len(self._decoded_buffer) == 0:
                return data

            if amt is None:
                data = self._decode(data, decode_content, flush_decoder)
                if cache_content:
                    self._body = data
            else:
                # do not waste memory on buffer when not decoding
                if not decode_content:
                    if self._has_decoded_content:
                        raise RuntimeError(
                            "Calling read(decode_content=False) is not supported after "
                            "read(decode_content=True) was called."
                        )
                    return data

                decoded_data = self._decode(data, decode_content, flush_decoder)
                self._decoded_buffer.put(decoded_data)

                while len(self._decoded_buffer) < amt and data:
                    # TODO make sure to initially read enough data to get past the headers
                    # For example, the GZ file header takes 10 bytes, we don't want to read
                    # it one byte at a time
                    if self._police_officer is not None:
                        async with self._police_officer.borrow(self):
                            data = await self._raw_read(amt)
                    else:
                        data = await self._raw_read(amt)

                    decoded_data = self._decode(data, decode_content, flush_decoder)
                    self._decoded_buffer.put(decoded_data)
                data = self._decoded_buffer.get(amt)

            return data
        finally:
            if (
                self._fp
                and hasattr(self._fp, "_eot")
                and self._fp._eot
                and self._police_officer is not None
            ):
                # an HTTP extension could be live, we don't want to accidentally kill it!
                if (
                    not hasattr(self._fp, "_dsa")
                    or self._fp._dsa is None
                    or self._fp._dsa.closed is True
                ):
                    self._police_officer.forget(self)
                    self._police_officer = None

    async def stream(  # type: ignore[override]
        self, amt: int | None = 2**16, decode_content: bool | None = None
    ) -> typing.AsyncGenerator[bytes, None]:
        if self._fp is None:
            return
        while not is_fp_closed(self._fp) or len(self._decoded_buffer) > 0:
            data = await self.read(amt=amt, decode_content=decode_content)

            if data:
                yield data

    async def close(self) -> None:  # type: ignore[override]
        if self.extension is not None and self.extension.closed is False:
            await self.extension.close()

        if not self.closed and self._fp:
            self._fp.close()

        if self._connection:
            await self._connection.close()

        if not self.auto_close:
            io.IOBase.close(self)

    async def __aiter__(self) -> typing.AsyncIterator[bytes]:
        buffer: list[bytes] = []
        async for chunk in self.stream(-1, decode_content=True):
            if b"\n" in chunk:
                chunks = chunk.split(b"\n")
                yield b"".join(buffer) + chunks[0] + b"\n"
                for x in chunks[1:-1]:
                    yield x + b"\n"
                if chunks[-1]:
                    buffer = [chunks[-1]]
                else:
                    buffer = []
            else:
                buffer.append(chunk)
        if buffer:
            yield b"".join(buffer)

    def __del__(self) -> None:
        if not self.closed:
            if not self.closed and self._fp:
                self._fp.close()

            if not self.auto_close:
                io.IOBase.close(self)
