from __future__ import annotations

import sys
from io import BytesIO
from test import ImportBlocker, ModuleStash, USING_SECONDARY_ENTRYPOINT

import pytest

zlib_blocker = ImportBlocker("zlib")
package_name = "urllib3" if not USING_SECONDARY_ENTRYPOINT else "urllib3_future"
module_stash = ModuleStash(package_name)


class TestWithoutZlib:
    @classmethod
    def setup_class(cls) -> None:
        sys.modules.pop("zlib", None)
        module_stash.stash()
        sys.meta_path.insert(0, zlib_blocker)

    @classmethod
    def teardown_class(cls) -> None:
        sys.meta_path.remove(zlib_blocker)
        module_stash.pop()

    def test_cannot_import_zlib(self) -> None:
        with pytest.raises(ImportError):
            import zlib  # noqa: F401

    def test_import_urllib3(self) -> None:
        __import__(package_name)

    def test_does_not_advertise_or_decode_zlib_encodings(self) -> None:
        request = __import__(
            f"{package_name}.util.request", fromlist=["ACCEPT_ENCODING"]
        )
        response = __import__(f"{package_name}.response", fromlist=["HTTPResponse"])

        advertised = request.ACCEPT_ENCODING.split(",")
        assert "gzip" not in advertised
        assert "deflate" not in advertised
        assert "gzip" not in response.HTTPResponse.CONTENT_DECODERS
        assert "deflate" not in response.HTTPResponse.CONTENT_DECODERS
        assert request.make_headers(accept_encoding=True)["accept-encoding"] == (
            request.ACCEPT_ENCODING
        )

        encoded = b"not decoded without zlib"
        http_response = response.HTTPResponse(
            body=BytesIO(encoded),
            headers={"content-encoding": "gzip"},
            preload_content=False,
        )
        assert http_response.read(decode_content=True) == encoded
