from __future__ import annotations

import io
import json as _json
import logging
import re
import sys
import typing
import warnings
import zlib
from contextlib import contextmanager
from socket import timeout as SocketTimeout

try:
    try:
        import brotlicffi as brotli  # type: ignore[import-not-found]
    except ImportError:
        import brotli  # type: ignore[import-not-found]
except ImportError:
    brotli = None

try:
    import zstandard as zstd  # type: ignore[import-not-found]

    # The package 'zstandard' added the 'eof' property starting
    # in v0.18.0 which we require to ensure a complete and
    # valid zstd stream was fed into the ZstdDecoder.
    # See: https://github.com/urllib3/urllib3/pull/2624
    _zstd_version = tuple(
        map(int, re.search(r"^([0-9]+)\.([0-9]+)", zstd.__version__).groups())  # type: ignore[union-attr]
    )
    if _zstd_version < (0, 18):  # Defensive:
        zstd = None

except (AttributeError, ImportError, ValueError):  # Defensive:
    try:
        from compression import zstd  # type: ignore[import-not-found]
    except ImportError:
        zstd = None

from ._collections import HTTPHeaderDict
from ._typing import _TYPE_BODY
from .backend import LowLevelResponse, ResponsePromise
from .exceptions import (
    BaseSSLError,
    DecodeError,
    HTTPError,
    IncompleteRead,
    InvalidHeader,
    ProtocolError,
    ReadTimeoutError,
    ResponseNotReady,
    SSLError,
)
from .util.response import is_fp_closed, BytesQueueBuffer
from .util.retry import Retry

if typing.TYPE_CHECKING:
    from email.message import Message

    from typing_extensions import Literal

    from .connection import HTTPConnection
    from .connectionpool import HTTPConnectionPool
    from .contrib.webextensions import ExtensionFromHTTP
    from .util.traffic_police import TrafficPolice

log = logging.getLogger(__name__)


class ContentDecoder:
    def decompress(self, data: bytes) -> bytes:
        raise NotImplementedError()

    def flush(self) -> bytes:
        raise NotImplementedError()


class DeflateDecoder(ContentDecoder):
    def __init__(self) -> None:
        self._first_try = True
        self._data = b""
        self._obj = zlib.decompressobj()

    def decompress(self, data: bytes) -> bytes:
        if not data:
            return data

        if not self._first_try:
            return self._obj.decompress(data)

        self._data += data
        try:
            decompressed = self._obj.decompress(data)
            if decompressed:
                self._first_try = False
                self._data = None  # type: ignore[assignment]
            return decompressed
        except zlib.error:
            self._first_try = False
            self._obj = zlib.decompressobj(-zlib.MAX_WBITS)
            try:
                return self.decompress(self._data)
            finally:
                self._data = None  # type: ignore[assignment]

    def flush(self) -> bytes:
        return self._obj.flush()


class GzipDecoderState:
    FIRST_MEMBER = 0
    OTHER_MEMBERS = 1
    SWALLOW_DATA = 2


class GzipDecoder(ContentDecoder):
    def __init__(self) -> None:
        self._obj = zlib.decompressobj(16 + zlib.MAX_WBITS)
        self._state = GzipDecoderState.FIRST_MEMBER

    def decompress(self, data: bytes) -> bytes:
        ret = bytearray()
        if self._state == GzipDecoderState.SWALLOW_DATA or not data:
            return bytes(ret)
        while True:
            try:
                ret += self._obj.decompress(data)
            except zlib.error:
                previous_state = self._state
                # Ignore data after the first error
                self._state = GzipDecoderState.SWALLOW_DATA
                if previous_state == GzipDecoderState.OTHER_MEMBERS:
                    # Allow trailing garbage acceptable in other gzip clients
                    return bytes(ret)
                raise
            data = self._obj.unused_data
            if not data:
                return bytes(ret)
            self._state = GzipDecoderState.OTHER_MEMBERS
            self._obj = zlib.decompressobj(16 + zlib.MAX_WBITS)

    def flush(self) -> bytes:
        return self._obj.flush()


if brotli is not None:

    class BrotliDecoder(ContentDecoder):
        # Supports both 'brotlipy' and 'Brotli' packages
        # since they share an import name. The top branches
        # are for 'brotlipy' and bottom branches for 'Brotli'
        def __init__(self) -> None:
            self._obj = brotli.Decompressor()
            if hasattr(self._obj, "decompress"):
                setattr(self, "decompress", self._obj.decompress)
            else:
                setattr(self, "decompress", self._obj.process)

        def flush(self) -> bytes:
            if hasattr(self._obj, "flush"):
                return self._obj.flush()  # type: ignore[no-any-return]
            return b""


if zstd is not None:
    # help distinguish external library from stdlib one
    _zstd_native = not hasattr(zstd.ZstdDecompressor, "decompressobj")

    class ZstdDecoder(ContentDecoder):
        def __init__(self) -> None:
            self._obj = (
                zstd.ZstdDecompressor().decompressobj()
                if not _zstd_native
                else zstd.ZstdDecompressor()
            )

        def decompress(self, data: bytes) -> bytes:
            if not data:
                return b""
            data_parts = [self._obj.decompress(data)]
            while self._obj.eof and self._obj.unused_data:
                unused_data = self._obj.unused_data
                self._obj = (
                    zstd.ZstdDecompressor().decompressobj()
                    if not _zstd_native
                    else zstd.ZstdDecompressor()
                )
                data_parts.append(self._obj.decompress(unused_data))
            return b"".join(data_parts)

        def flush(self) -> bytes:
            ret = self._obj.flush()  # note: this is a no-op
            if not self._obj.eof:
                raise DecodeError("Zstandard data is incomplete")
            return ret  # type: ignore[no-any-return]


class MultiDecoder(ContentDecoder):
    """
    From RFC7231:
        If one or more encodings have been applied to a representation, the
        sender that applied the encodings MUST generate a Content-Encoding
        header field that lists the content codings in the order in which
        they were applied.
    """

    def __init__(self, modes: str) -> None:
        self._decoders = [_get_decoder(m.strip()) for m in modes.split(",")]

    def flush(self) -> bytes:
        return self._decoders[0].flush()

    def decompress(self, data: bytes) -> bytes:
        for d in reversed(self._decoders):
            data = d.decompress(data)
        return data


def _get_decoder(mode: str) -> ContentDecoder:
    if "," in mode:
        return MultiDecoder(mode)

    if mode == "gzip":
        return GzipDecoder()

    if brotli is not None and mode == "br":
        return BrotliDecoder()

    if zstd is not None and mode == "zstd":
        return ZstdDecoder()

    return DeflateDecoder()


class HTTPResponse(io.IOBase):
    """
    HTTP Response container.

    Backwards-compatible with :class:`http.client.HTTPResponse` but the response ``body`` is
    loaded and decoded on-demand when the ``data`` property is accessed.  This
    class is also compatible with the Python standard library's :mod:`io`
    module, and can hence be treated as a readable object in the context of that
    framework.

    Extra parameters for behaviour not present in :class:`http.client.HTTPResponse`:

    :param preload_content:
        If True, the response's body will be preloaded during construction.

    :param decode_content:
        If True, will attempt to decode the body based on the
        'content-encoding' header.

    :param original_response:
        When this HTTPResponse wrapper is generated from an :class:`http.client.HTTPResponse`
        object, it's convenient to include the original for debug purposes. It's
        otherwise unused.

    :param retries:
        The retries contains the last :class:`~urllib3.util.retry.Retry` that
        was used during the request.

    :param enforce_content_length:
        Enforce content length checking. Body returned by server must match
        value of Content-Length header, if present. Otherwise, raise error.
    """

    CONTENT_DECODERS = ["gzip", "deflate"]
    if brotli is not None:
        CONTENT_DECODERS += ["br"]
    if zstd is not None:
        CONTENT_DECODERS += ["zstd"]
    REDIRECT_STATUSES = [301, 302, 303, 307, 308]

    DECODER_ERROR_CLASSES: tuple[type[Exception], ...] = (IOError, zlib.error)
    if brotli is not None:
        DECODER_ERROR_CLASSES += (brotli.error,)

    if zstd is not None:
        DECODER_ERROR_CLASSES += (zstd.ZstdError,)

    def __init__(
        self,
        body: _TYPE_BODY = "",
        headers: typing.Mapping[str, str] | typing.Mapping[bytes, bytes] | None = None,
        status: int = 0,
        version: int = 0,
        reason: str | None = None,
        preload_content: bool = True,
        decode_content: bool = True,
        original_response: LowLevelResponse | None = None,
        pool: HTTPConnectionPool | None = None,
        connection: HTTPConnection | None = None,
        msg: Message | None = None,
        retries: Retry | None = None,
        enforce_content_length: bool = True,
        request_method: str | None = None,
        request_url: str | None = None,
        auto_close: bool = True,
        police_officer: TrafficPolice[HTTPConnection] | None = None,
    ) -> None:
        if isinstance(headers, HTTPHeaderDict):
            self.headers = headers
        else:
            self.headers = HTTPHeaderDict(headers)  # type: ignore[arg-type]
        try:
            self.status = int(status)
        except ValueError:
            self.status = 0  # merely for tests, was supported due to broken httplib.

        # Mind this case for later on!
        if preload_content and status == 101:
            preload_content = False

        self.version = version
        self.reason = reason
        self.decode_content = decode_content
        self._has_decoded_content = False
        self._request_url: str | None = request_url
        self._retries: Retry | None = None

        self._extension: ExtensionFromHTTP | None = None

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
        self._fp: LowLevelResponse | typing.IO[typing.Any] | None = None
        self._original_response = original_response
        self._fp_bytes_read = 0

        self.msg = msg  # no-op, kept for BC.

        if body and isinstance(body, (str, bytes)):
            self._body = body

        self._pool = pool
        self._connection = connection

        if hasattr(body, "read"):
            self._fp = body  # type: ignore[assignment]

        # Are we using the chunked-style of transfer encoding?
        self.chunk_left: int | None = None

        # Determine length of response
        self._request_method: str | None = request_method
        self.length_remaining: int | None = self._init_length(self._request_method)

        # Used to return the correct amount of bytes for partial read()s
        self._decoded_buffer = BytesQueueBuffer()

        self._police_officer: TrafficPolice[HTTPConnection] | None = police_officer

        if self._police_officer is not None:
            self._police_officer.memorize(self, self._connection)
            if self._police_officer.parent is not None:
                self._police_officer.parent.memorize(self, self._pool)

        self._preloaded_content = preload_content

        # If requested, preload the body.
        if preload_content and not self._body:
            self._body = self.read(decode_content=decode_content)

    def is_from_promise(self, promise: ResponsePromise) -> bool:
        """
        Determine if this response came from given promise.
        """
        return (
            self._fp is not None
            and hasattr(self._fp, "from_promise")
            and self._fp.from_promise == promise
        )

    def get_redirect_location(self) -> str | None | Literal[False]:
        """
        Should we redirect and where to?

        :returns: Truthy redirect location string if we got a redirect status
            code and valid location. ``None`` if redirect status and no
            location. ``False`` if not a redirect status code.
        """
        if self.status in self.REDIRECT_STATUSES:
            return self.headers.get("location")
        return False

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
    def extension(self) -> ExtensionFromHTTP | None:
        return self._extension

    def start_extension(self, item: ExtensionFromHTTP) -> None:
        if self._extension is not None:
            raise OSError("extension already plugged in")

        if not hasattr(self._fp, "_dsa"):
            raise ResponseNotReady()

        item.start(self)

        self._extension = item

    def json(self) -> typing.Any:
        """
        Parses the body of the HTTP response as JSON.

        To use a custom JSON decoder pass the result of :attr:`HTTPResponse.data` to the decoder.

        This method can raise either `UnicodeDecodeError` or `json.JSONDecodeError`.

        Read more :ref:`here <json>`.
        """
        data = self.data.decode("utf-8")
        return _json.loads(data)

    @property
    def retries(self) -> Retry | None:
        return self._retries

    @retries.setter
    def retries(self, retries: Retry | None) -> None:
        # Override the request_url if retries has a redirect location.
        if retries is not None and retries.history:
            self.url = retries.history[-1].redirect_location
        self._retries = retries

    def _init_decoder(self) -> None:
        """
        Set-up the _decoder attribute if necessary.
        """
        # Note: content-encoding value should be case-insensitive, per RFC 7230
        # Section 3.2
        if "content-encoding" not in self.headers:
            return
        content_encoding = self.headers.get("content-encoding", "").lower()
        if self._decoder is None:
            if content_encoding in self.CONTENT_DECODERS:
                self._decoder = _get_decoder(content_encoding)
            elif "," in content_encoding:
                encodings = [
                    e.strip()
                    for e in content_encoding.split(",")
                    if e.strip() in self.CONTENT_DECODERS
                ]
                if encodings:
                    self._decoder = _get_decoder(content_encoding)

    def _decode(
        self, data: bytes, decode_content: bool | None, flush_decoder: bool
    ) -> bytes:
        """
        Decode the data passed in and potentially flush the decoder.
        """
        if not decode_content:
            if self._has_decoded_content:
                raise RuntimeError(
                    "Calling read(decode_content=False) is not supported after "
                    "read(decode_content=True) was called."
                )
            return data

        try:
            if self._decoder:
                data = self._decoder.decompress(data)
                self._has_decoded_content = True
        except self.DECODER_ERROR_CLASSES as e:
            content_encoding = self.headers.get("content-encoding", "").lower()
            raise DecodeError(
                "Received response with content-encoding: %s, but "
                "failed to decode it." % content_encoding,
                e,
            ) from e
        if flush_decoder:
            data += self._flush_decoder()

        return data

    def _flush_decoder(self) -> bytes:
        """
        Flushes the decoder. Should only be called if the decoder is actually
        being used.
        """
        if self._decoder:
            return self._decoder.decompress(b"") + self._decoder.flush()
        return b""

    # Compatibility methods for `io` module
    def readinto(self, b: bytearray) -> int:
        temp = self.read(len(b))
        if len(temp) == 0:
            return 0
        else:
            b[: len(temp)] = temp
            return len(temp)

    # Compatibility method for http.cookiejar
    def info(self) -> HTTPHeaderDict:
        return self.headers

    def geturl(self) -> str | None:
        return self.url

    def release_conn(self) -> None:
        if not self._connection:
            return None

        if (
            self._police_officer is not None
            and self._police_officer.is_held(self._connection) is True
        ):
            self._police_officer.release()

        self._connection = None

    def drain_conn(self) -> None:
        """
        Read and discard any remaining HTTP response data in the response connection.

        Unread data in the HTTPResponse connection blocks the connection from being released back to the pool.
        """
        try:
            self.read()
        except (HTTPError, OSError, BaseSSLError):
            pass

    @property
    def data(self) -> bytes:
        # For backwards-compat with earlier urllib3 0.4 and earlier.
        if self._body:
            return self._body  # type: ignore[return-value]

        if self._fp:
            return self.read(cache_content=True)

        return None  # type: ignore[return-value]

    @property
    def connection(self) -> HTTPConnection | None:
        return self._connection

    def isclosed(self) -> bool:
        return is_fp_closed(self._fp)

    def tell(self) -> int:
        """
        Obtain the number of bytes pulled over the wire so far. May differ from
        the amount of content returned by :meth:``urllib3.response.HTTPResponse.read``
        if bytes are encoded on the wire (e.g, compressed).
        """
        return self._fp_bytes_read

    def _init_length(self, request_method: str | None) -> int | None:
        """
        Set initial length value for Response content if available.
        """
        length: int | None
        content_length: str | None = self.headers.get("content-length")

        if content_length is not None:
            if self.chunked:
                # This Response will fail with an IncompleteRead if it can't be
                # received as chunked. This method falls back to attempt reading
                # the response before raising an exception.
                log.warning(
                    "Received response with both Content-Length and "
                    "Transfer-Encoding set. This is expressly forbidden "
                    "by RFC 7230 sec 3.3.2. Ignoring Content-Length and "
                    "attempting to process response as Transfer-Encoding: "
                    "chunked."
                )
                return None

            try:
                # RFC 7230 section 3.3.2 specifies multiple content lengths can
                # be sent in a single Content-Length header
                # (e.g. Content-Length: 42, 42). This line ensures the values
                # are all valid ints and that as long as the `set` length is 1,
                # all values are the same. Otherwise, the header is invalid.
                if "," in content_length:
                    lengths = {int(val) for val in content_length.split(",")}
                    if len(lengths) > 1:
                        raise InvalidHeader(
                            "Content-Length contained multiple "
                            "unmatching values (%s)" % content_length
                        )
                    length = lengths.pop()
                else:
                    length = int(content_length)
            except ValueError:
                length = None
            else:
                if length < 0:
                    length = None

        else:  # if content_length is None
            length = None

        # Check for responses that shouldn't include a body
        if (
            self.status in (204, 304)
            or 100 <= self.status < 200
            or request_method == "HEAD"
        ):
            length = 0

        return length

    @contextmanager
    def _error_catcher(self) -> typing.Generator[None, None, None]:
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
                    self._connection.close()

            # If we hold the original response but it's closed now, we should
            # return the connection back to the pool.
            if self._original_response and self._original_response.isclosed():
                self.release_conn()

    def _fp_read(self, amt: int | None = None) -> bytes:
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
                    data = self._fp.read(chunk_amt)
                except ValueError:  # Defensive: overly protective
                    break  # Defensive: can also be an indicator that read ended, should not happen.
                if not data:
                    break
                buffer.write(data)
                del data  # to reduce peak memory usage by `max_chunk_amt`.
            return buffer.getvalue()
        else:
            # StringIO doesn't like amt=None
            return self._fp.read(amt) if amt is not None else self._fp.read()

    def _raw_read(
        self,
        amt: int | None = None,
    ) -> bytes:
        """
        Reads `amt` of bytes from the socket.
        """
        if self._fp is None:
            return None  # type: ignore[return-value]

        fp_closed = getattr(self._fp, "closed", False)

        with self._error_catcher():
            data = self._fp_read(amt) if not fp_closed else b""

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

    def read1(
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

        data = self.read(
            amt=amt or -1,
            decode_content=decode_content,
        )

        if amt is not None and len(data) > amt:
            self._decoded_buffer.put(data)
            return self._decoded_buffer.get(amt)

        return data

    def read(
        self,
        amt: int | None = None,
        decode_content: bool | None = None,
        cache_content: bool = False,
    ) -> bytes:
        """
        Similar to :meth:`http.client.HTTPResponse.read`, but with two additional
        parameters: ``decode_content`` and ``cache_content``.

        :param amt:
            How much of the content to read. If specified, caching is skipped
            because it doesn't make sense to cache partial content as the full
            response.

        :param decode_content:
            If True, will attempt to decode the body based on the
            'content-encoding' header.

        :param cache_content:
            If True, will save the returned data such that the same result is
            returned despite of the state of the underlying file object. This
            is useful if you want the ``.data`` property to continue working
            after having ``.read()`` the file object. (Overridden if ``amt`` is
            set.)
        """
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
                with self._police_officer.borrow(self):
                    data = self._raw_read(amt)
            else:
                data = self._raw_read(amt)

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
                        with self._police_officer.borrow(self):
                            data = self._raw_read(amt)
                    else:
                        data = self._raw_read(amt)

                    decoded_data = self._decode(data, decode_content, flush_decoder)
                    self._decoded_buffer.put(decoded_data)
                data = self._decoded_buffer.get(amt)

            return data
        finally:
            if (
                hasattr(self._fp, "_eot")
                and self._fp._eot  # type: ignore[union-attr]
                and self._police_officer is not None
            ):
                # an HTTP extension could be live, we don't want to accidentally kill it!
                if (
                    not hasattr(self._fp, "_dsa")
                    or self._fp._dsa is None  # type: ignore[union-attr]
                    or self._fp._dsa.closed is True  # type: ignore[union-attr]
                ):
                    self._police_officer.forget(self)
                    self._police_officer = None

    def stream(
        self, amt: int | None = 2**16, decode_content: bool | None = None
    ) -> typing.Generator[bytes, None, None]:
        """
        A generator wrapper for the read() method. A call will block until
        ``amt`` bytes have been read from the connection or until the
        connection is closed.

        :param amt:
            How much of the content to read. The generator will return up to
            much data per iteration, but may return less. This is particularly
            likely when using compressed data. However, the empty string will
            never be returned. Setting -1 will output chunks as soon as they
            arrive.

        :param decode_content:
            If True, will attempt to decode the body based on the
            'content-encoding' header.
        """
        if self._fp is None:
            return
        while not is_fp_closed(self._fp) or len(self._decoded_buffer) > 0:
            data = self.read(amt=amt, decode_content=decode_content)

            if data:
                yield data

    # Overrides from io.IOBase
    def readable(self) -> bool:
        return True

    def close(self) -> None:
        if self.extension is not None and self.extension.closed is False:
            self.extension.close()

        if not self.closed and self._fp:
            self._fp.close()

        if self._connection:
            self._connection.close()

        if not self.auto_close:
            io.IOBase.close(self)

    @property
    def closed(self) -> bool:
        if not self.auto_close:
            return io.IOBase.closed.__get__(self)  # type: ignore[no-any-return]
        elif self._fp is None:
            return True
        elif hasattr(self._fp, "isclosed"):
            return self._fp.isclosed()
        elif hasattr(self._fp, "closed"):
            return self._fp.closed
        else:
            return True

    def fileno(self) -> int:
        if self._fp is None:
            raise OSError("HTTPResponse has no file to get a fileno from")
        elif hasattr(self._fp, "fileno"):
            return self._fp.fileno()
        else:
            raise OSError(
                "The file-like object this HTTPResponse is wrapped "
                "around has no file descriptor"
            )

    def flush(self) -> None:
        if (
            self._fp is not None
            and hasattr(self._fp, "flush")
            and not getattr(self._fp, "closed", False)
        ):
            return self._fp.flush()  # type: ignore[return-value]

    def supports_chunked_reads(self) -> bool:
        """
        Checks if the underlying file-like object looks like a
        :class:`http.client.HTTPResponse` object. We do this by testing for
        the fp attribute. If it is present we assume it returns raw chunks as
        processed by read_chunked().
        """
        return False

    @property
    def url(self) -> str | None:
        """
        Returns the URL that was the source of this response.
        If the request that generated this response redirected, this method
        will return the final redirect location.
        """
        return self._request_url

    @url.setter
    def url(self, url: str) -> None:
        self._request_url = url

    # Compatibility methods for http.client.HTTPResponse
    def getheaders(self) -> HTTPHeaderDict:
        warnings.warn(
            "HTTPResponse.getheaders() is deprecated and will be removed "
            "in a future version of urllib3(-future). Instead access HTTPResponse.headers directly.",
            category=DeprecationWarning,
            stacklevel=2,
        )
        return self.headers

    def getheader(self, name: str, default: str | None = None) -> str | None:
        warnings.warn(
            "HTTPResponse.getheader() is deprecated and will be removed "
            "in a future version of urllib3(-future). Instead use HTTPResponse.headers.get(name, default).",
            category=DeprecationWarning,
            stacklevel=2,
        )
        return self.headers.get(name, default)

    def __iter__(self) -> typing.Iterator[bytes]:
        buffer: list[bytes] = []
        for chunk in self.stream(-1, decode_content=True):
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

    def shutdown(self) -> None:
        """urllib3 implemented this method in version 2.3 to palliate for a
        thread safety issue[...] using another thread safety issue[...]
        fortunately, we don't need that hack with urllib3-future thanks to
        our extensive safety with TrafficPolice. You may safely remove that
        call."""
        pass


# Kept for BC-purposes.
BaseHTTPResponse = HTTPResponse
