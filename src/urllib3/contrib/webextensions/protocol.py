from __future__ import annotations

import typing
from abc import ABCMeta

if typing.TYPE_CHECKING:
    from ...backend import HttpVersion
    from ...backend._base import DirectStreamAccess
    from ...response import HTTPResponse
    from ...util.traffic_police import TrafficPolice


class ExtensionFromHTTP(metaclass=ABCMeta):
    """Represent an extension that can be negotiated just after a "101 Switching Protocol" HTTP response.
    This will considerably ease downstream integration."""

    def __init__(self) -> None:
        self._dsa: DirectStreamAccess | None = None
        self._response: HTTPResponse | None = None
        self._police_officer: TrafficPolice | None = None  # type: ignore[type-arg]

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
