"""
urllib3.contrib.anytls
======================

Single point of resolution for the active TLS backend.

This module masks the conditional ``import rtls as ssl`` / ``import utls as
ssl`` / ``import ssl`` dance that would otherwise be duplicated in every
module that needs TLS. It picks the best available backend at import time
following the default priority:

    rtls (Rustls + AWS-LC)  ->  utls (BoringSSL)  ->  ssl (stdlib)
"""

from __future__ import annotations

import typing

from ._backend import _resolve

if typing.TYPE_CHECKING:
    # For static type-checkers, expose the stdlib ``ssl`` module. All three
    # backends share the same surface used by urllib3, so this is safe.
    import ssl

    stdlib_ssl = ssl
    Certificate: typing.Any = None
    BACKEND: str = "ssl"
    HAS_SSL: bool = True
    IS_NONSTDLIB: bool = False
else:
    ssl, stdlib_ssl, BACKEND, Certificate = _resolve()
    HAS_SSL = ssl is not None
    IS_NONSTDLIB = BACKEND in ("rtls", "utls")


__all__ = (
    "ssl",
    "stdlib_ssl",
    "BACKEND",
    "HAS_SSL",
    "IS_NONSTDLIB",
    "Certificate",
)
