"""
This is meant to support various HTTP version.

    - (standard) http.client shipped within cpython distribution
    - (experimental) hface shipped by installing urllib3-ext-hface
"""

from __future__ import annotations

from ._base import BaseBackend, HttpVersion, LowLevelResponse, QuicPreemptiveCacheType
from .hface import HfaceBackend

__all__ = (
    "BaseBackend",
    "HfaceBackend",
    "HttpVersion",
    "QuicPreemptiveCacheType",
    "LowLevelResponse",
)
