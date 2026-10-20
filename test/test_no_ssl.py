"""
Test what happens if Python was built without SSL

* Everything that does not involve HTTPS should still work
* HTTPS requests must fail with an error that points at the ssl module
"""

from __future__ import annotations

import sys
from test import ImportBlocker, ModuleStash, USING_SECONDARY_ENTRYPOINT

import pytest

ssl_blocker = ImportBlocker("ssl", "_ssl")
module_stash = ModuleStash(
    "urllib3" if not USING_SECONDARY_ENTRYPOINT else "urllib3_future"
)
ssl_module_stash = ModuleStash("ssl")
native_ssl_module_stash = ModuleStash("_ssl")


class TestWithoutSSL:
    @classmethod
    def setup_class(cls) -> None:
        ssl_module_stash.stash()
        native_ssl_module_stash.stash()

        module_stash.stash()
        sys.meta_path.insert(0, ssl_blocker)

    @classmethod
    def teardown_class(cls) -> None:
        sys.meta_path.remove(ssl_blocker)
        # Reloading _ssl recreates exception classes used by existing imports.
        native_ssl_module_stash.pop()
        ssl_module_stash.pop()
        module_stash.pop()


class TestImportWithoutSSL(TestWithoutSSL):
    def test_cannot_import_ssl(self) -> None:
        with pytest.raises(ImportError):
            import ssl  # noqa: F401

    def test_import_urllib3(self) -> None:
        import urllib3  # noqa: F401
