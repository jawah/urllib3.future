# Dummy file to match upstream modules
# without actually serving them.
# urllib3-future diverged from urllib3.
# only the top-level (public API) are guaranteed to be compatible.
# in-fact urllib3-future propose a better way to migrate/transition toward
# newer protocols.

from __future__ import annotations

import warnings


def inject_into_urllib3() -> None:
    warnings.warn(
        "urllib3-future do not propose the http2 module as it is useless to us. "
        "enjoy all three protocols. urllib3-future just works out of the box with all protocols.",
        UserWarning,
    )


def extract_from_urllib3() -> None:
    pass
