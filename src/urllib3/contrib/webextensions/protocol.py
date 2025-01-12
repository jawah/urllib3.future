from __future__ import annotations

import typing
from abc import ABCMeta
from contextlib import contextmanager
from socket import timeout as SocketTimeout

if typing.TYPE_CHECKING:
    from ...backend import HttpVersion
    from ...backend._base import DirectStreamAccess
    from ...response import HTTPResponse
    from ...util.traffic_police import TrafficPolice

from ...exceptions import BaseSSLError, ProtocolError, ReadTimeoutError, SSLError


class ExtensionFromHTTP(metaclass=ABCMeta):
    """Represent an extension that can be negotiated just after a "101 Switching Protocol" HTTP response.
    This will considerably ease downstream integration."""

    def __init__(self) -> None:
        self._dsa: DirectStreamAccess | None = None
        self._response: HTTPResponse | None = None
        self._police_officer: TrafficPolice | None = None  # type: ignore[type-arg]

    @contextmanager
    def _read_error_catcher(self) -> typing.Generator[None, None, None]:
        """
        Catch low-level python exceptions, instead re-raising urllib3
        variants, so that low-level exceptions are not leaked in the
        high-level api.

        On unrecoverable issues, release the connection back to the pool.
        """
        clean_exit = False

        try:
            try:
                yield

            except SocketTimeout as e:
                clean_exit = True
                pool = (
                    self._response._pool  # type: ignore[has-type]
                    if self._response and hasattr(self._response, "_pool")
                    else None
                )
                raise ReadTimeoutError(pool, None, "Read timed out.") from e  # type: ignore[arg-type]

            except BaseSSLError as e:
                # FIXME: Is there a better way to differentiate between SSLErrors?
                if "read operation timed out" not in str(e):
                    # SSL errors related to framing/MAC get wrapped and reraised here
                    raise SSLError(e) from e
                clean_exit = True  # ws algorithms based on timeouts can expect this without being harmful!
                pool = (
                    self._response._pool  # type: ignore[has-type]
                    if self._response and hasattr(self._response, "_pool")
                    else None
                )
                raise ReadTimeoutError(pool, None, "Read timed out.") from e  # type: ignore[arg-type]

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
                if self._response:
                    self.close()

    @contextmanager
    def _write_error_catcher(self) -> typing.Generator[None, None, None]:
        """
        Catch low-level python exceptions, instead re-raising urllib3
        variants, so that low-level exceptions are not leaked in the
        high-level api.

        On unrecoverable issues, release the connection back to the pool.
        """
        clean_exit = False

        try:
            try:
                yield

            except SocketTimeout as e:
                pool = (
                    self._response._pool  # type: ignore[has-type]
                    if self._response and hasattr(self._response, "_pool")
                    else None
                )
                raise ReadTimeoutError(pool, None, "Read timed out.") from e  # type: ignore[arg-type]

            except BaseSSLError as e:
                raise SSLError(e) from e

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
                if self._response:
                    self.close()

    @property
    def urlopen_kwargs(self) -> dict[str, typing.Any]:
        """Return prerequisites. Must be passed as additional parameters to urlopen."""
        return {}

    def start(self, response: HTTPResponse) -> None:
        """The HTTP server gave us the go-to start negotiating another protocol."""
        if response._fp is None or not hasattr(response._fp, "_dsa"):
            raise RuntimeError(
                "Attempt to start an HTTP extension without direct I/O access to the stream"
            )

        self._dsa = response._fp._dsa
        self._police_officer = response._police_officer
        self._response = response

    @property
    def closed(self) -> bool:
        return self._dsa is None

    @staticmethod
    def supported_svn() -> set[HttpVersion]:
        """Hint about supported parent SVN for this extension."""
        raise NotImplementedError

    @staticmethod
    def implementation() -> str:
        raise NotImplementedError

    @staticmethod
    def supported_schemes() -> set[str]:
        """Recognized schemes for the extension."""
        raise NotImplementedError

    @staticmethod
    def scheme_to_http_scheme(scheme: str) -> str:
        """Convert the extension scheme to a known http scheme (either http or https)"""
        raise NotImplementedError

    def headers(self, http_version: HttpVersion) -> dict[str, str]:
        """Specific HTTP headers required (request) before the 101 status response."""
        raise NotImplementedError

    def close(self) -> None:
        """End/Notify close for sub protocol."""
        raise NotImplementedError

    def next_payload(self) -> str | bytes | None:
        """Unpack the next received message/payload from remote. This call does read from the socket.
        If the method return None, it means that the remote closed the (extension) pipeline.
        """
        raise NotImplementedError

    def send_payload(self, buf: str | bytes) -> None:
        """Dispatch a buffer to remote."""
        raise NotImplementedError

    def on_payload(self, callback: typing.Callable[[str | bytes | None], None]) -> None:
        """Set up a callback that will be invoked automatically once a payload is received.
        Meaning that you stop calling manually next_payload()."""
        raise NotImplementedError
