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
        (
            "urllib3-future does not support WASM / Emscripten platform. "
            "Please reinstall legacy urllib3 in the meantime. "
            "Run `pip uninstall -y urllib3 urllib3-future` then "
            "`pip install urllib3-future`, finally `pip install urllib3`. "
            "Sorry for the inconvenience."
        ),
        DeprecationWarning,
    )


def extract_from_urllib3() -> None:
    pass
