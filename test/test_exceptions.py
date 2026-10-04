from __future__ import annotations

import pickle
import socket
from email.errors import MessageDefect
from test import DUMMY_POOL

import pytest

from urllib3.connection import HTTPConnection
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.exceptions import (
    ClosedPoolError,
    ConnectTimeoutError,
    EmptyPoolError,
    HeaderParsingError,
    HostChangedError,
    HTTPError,
    LocationParseError,
    MaxRetryError,
    NameResolutionError,
    NewConnectionError,
    ReadTimeoutError,
)


class TestPickle:
    @pytest.mark.parametrize(
        "exception",
        [
            HTTPError(None),
            MaxRetryError(DUMMY_POOL, "", None),
            LocationParseError(""),
            ConnectTimeoutError(None),
            HTTPError("foo"),
            HTTPError("foo", IOError("foo")),
            MaxRetryError(HTTPConnectionPool("localhost"), "/", None),
            LocationParseError("fake location"),
            ClosedPoolError(HTTPConnectionPool("localhost"), ""),
            EmptyPoolError(HTTPConnectionPool("localhost"), ""),
            HostChangedError(HTTPConnectionPool("localhost"), "/", 0),
            ReadTimeoutError(HTTPConnectionPool("localhost"), "/", ""),
            NewConnectionError(HTTPConnection("localhost"), ""),
            NameResolutionError("", HTTPConnection("localhost"), socket.gaierror()),
        ],
    )
    def test_exceptions(self, exception: Exception) -> None:
        result = pickle.loads(pickle.dumps(exception))
        assert isinstance(result, type(exception))


class TestFormat:
    def test_incomplete_read_without_expected_length(self) -> None:
        from urllib3.exceptions import IncompleteRead

        assert repr(IncompleteRead(17, None)) == "IncompleteRead(17 bytes read)"

    def test_invalid_chunk_length(self) -> None:
        from io import BytesIO
        from urllib3 import HTTPResponse
        from urllib3.exceptions import InvalidChunkLength

        response = HTTPResponse(body=BytesIO(b"hello"), preload_content=False)
        assert response.read(2) == b"he"
        error = InvalidChunkLength(response, b"not-hex\r\n")
        assert error.response is response
        assert error.partial == 2
        assert error.expected is None
        assert (
            repr(error)
            == "InvalidChunkLength(got length b'not-hex\\r\\n', 2 bytes read)"
        )

    def test_header_parsing_errors(self) -> None:
        hpe = HeaderParsingError([MessageDefect("defects")], "unparsed_data")

        assert "defects" in str(hpe)
        assert "unparsed_data" in str(hpe)
