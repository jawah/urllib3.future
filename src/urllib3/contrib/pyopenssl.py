from __future__ import annotations

import warnings

warnings.warn(
    "'urllib3.contrib.pyopenssl' module has been removed "
    "in urllib3 v2.1.0 due to incompatibilities with our QUIC integration."
    "While the import still work, it is rendered completely ineffective.",
    category=DeprecationWarning,
    stacklevel=2,
)

__all__ = ["inject_into_urllib3", "extract_from_urllib3"]


def inject_into_urllib3() -> None:
    """Kept for BC-purposes."""
    ...


def extract_from_urllib3() -> None:
    """Kept for BC-purposes."""
    ...
