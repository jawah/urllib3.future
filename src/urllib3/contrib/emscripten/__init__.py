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
        "urllib3-future do not have a emscripten module as it is irrelevant to urllib3 nature. "
        "wasm support will be brought in Niquests (replacement for Requests). "
        "One does not simply ship an addon that essentially kills 90% of its other features and alter the 10 "
        "remaining percents.",
        UserWarning,
    )


def extract_from_urllib3() -> None:
    pass
