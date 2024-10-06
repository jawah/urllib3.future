from __future__ import annotations

from .protocol import ExtensionFromHTTP
from .raw import RawExtensionFromHTTP

try:
    from .ws import WebSocketExtensionFromHTTP, WebSocketExtensionFromMultiplexedHTTP
except ImportError:
    WebSocketExtensionFromHTTP = None  # type: ignore[misc, assignment]
    WebSocketExtensionFromMultiplexedHTTP = None  # type: ignore[misc, assignment]


def load_extension(
    scheme: str | None, implementation: str | None = None
) -> type[ExtensionFromHTTP]:
    if scheme is None:
        return RawExtensionFromHTTP

    for extension in ExtensionFromHTTP.__subclasses__():
        if scheme in extension.supported_schemes():
            if (
                implementation is not None
                and extension.implementation() != implementation
            ):
                continue
            return extension

    raise ImportError(
        f"Tried to load HTTP extension '{scheme}' but no available plugin support it."
    )


__all__ = (
    "ExtensionFromHTTP",
    "RawExtensionFromHTTP",
    "WebSocketExtensionFromHTTP",
    "WebSocketExtensionFromMultiplexedHTTP",
    "load_extension",
)
