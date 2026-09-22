"""Packaging regressions; optionally exercise the installed cohabitation wheel."""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import zipfile

from isolation import index, restore, wheel_metadata


class IsolationIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.site = self.root / "site"

    def wheel(
        self,
        version: str,
        extra: str | None = None,
        *,
        runtime_version: str | None = None,
    ) -> Path:
        wheel = self.root / f"urllib3_future-{version}-py3-none-any.whl"
        dist = f"urllib3_future-{version}.dist-info"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr(
                "urllib3_future/_version.py",
                f'__version__ = "{runtime_version or version.partition("+")[0]}"\n',
            )
            archive.writestr(
                f"{dist}/METADATA",
                "Metadata-Version: 2.4\nName: urllib3-future\n"
                f"Version: {version}\nRequires-Python: >=3.7\n",
            )
            if extra:
                archive.writestr(extra, "unexpected")
        return wheel

    def test_rejects_dropin_contents(self) -> None:
        for extra in (
            "urllib3/__init__.py",
            "urllib3_future.pth",
            "urllib3_future/hidden.pth",
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                wheel_metadata(self.wheel("2.25.900+isolation", extra))

    def test_rejects_public_version(self) -> None:
        with self.assertRaises(ValueError):
            wheel_metadata(self.wheel("2.25.900"))

    def test_rejects_local_runtime_version(self) -> None:
        with self.assertRaisesRegex(ValueError, "Package and wheel versions disagree"):
            wheel_metadata(
                self.wheel("2.25.900+isolation", runtime_version="2.25.900+isolation")
            )

    def test_immutable_and_idempotent(self) -> None:
        wheel = self.wheel("2.25.900+isolation")
        index(self.site, wheel)
        before = {
            p.relative_to(self.site): p.read_bytes()
            for p in self.site.rglob("*")
            if p.is_file()
        }
        index(self.site, wheel)
        self.assertEqual(
            before,
            {
                p.relative_to(self.site): p.read_bytes()
                for p in self.site.rglob("*")
                if p.is_file()
            },
        )
        with zipfile.ZipFile(wheel, "a") as archive:
            archive.writestr("urllib3_future/changed.py", "changed")
        with self.assertRaisesRegex(ValueError, "Refusing to replace"):
            index(self.site, wheel)

    def test_keeps_history_with_hashes_and_encoded_links(self) -> None:
        for version in ("2.25.901+isolation", "2.25.900+isolation"):
            wheel = self.wheel(version)
            index(self.site, wheel)
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            text = (
                self.site / "isolation/simple/urllib3-future/index.html"
            ).read_text()
            self.assertIn(wheel.name.replace("+", "%2B") + "#sha256=" + digest, text)
            self.assertIn('data-requires-python="&gt;=3.7"', text)
            landing = (self.site / "isolation/index.html").read_text()
            self.assertIn(
                "https://github.com/jawah/urllib3.future/releases/download/"
                f"{version.removesuffix('+isolation')}/multiple.intoto.jsonl",
                landing,
            )
        self.assertLess(text.index("2.25.901"), text.index("2.25.900"))

    def test_restore_release_assets(self) -> None:
        wheels = [self.wheel(f"2.25.{patch}+isolation") for patch in (900, 901)]
        releases = self.root / "releases.json"
        assets = [{"name": "urllib3_future-2.25.901-py3-none-any.whl"}]
        for wheel in wheels:
            assets.append(
                {
                    "name": wheel.name.replace("+", "."),
                    "browser_download_url": wheel.as_uri(),
                    "digest": "sha256:"
                    + hashlib.sha256(wheel.read_bytes()).hexdigest(),
                }
            )
        releases.write_text(json.dumps(assets))
        restore(self.site, releases)
        restore(self.site, releases)
        project = self.site / "isolation/simple/urllib3-future"
        self.assertEqual(
            sorted(p.name for p in project.iterdir()), [w.name for w in wheels]
        )
        for wheel in wheels:
            self.assertEqual((project / wheel.name).read_bytes(), wheel.read_bytes())
        index(self.site, wheels[-1])
        for wheel in wheels:
            self.assertIn(
                wheel.name.replace("+", "%2B"), (project / "index.html").read_text()
            )

    def test_restore_rejects_asset_digest_mismatch(self) -> None:
        wheel = self.wheel("2.25.900+isolation")
        releases = self.root / "releases.json"
        releases.write_text(
            json.dumps(
                [
                    {
                        "name": wheel.name,
                        "browser_download_url": wheel.as_uri(),
                        "digest": "sha256:" + "0" * 64,
                    }
                ]
            )
        )
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            restore(self.site, releases)
        self.assertFalse(
            (self.site / "isolation/simple/urllib3-future" / wheel.name).exists()
        )


def installed_smoke() -> None:
    from importlib.metadata import version

    import niquests
    import requests
    import urllib3
    import urllib3_future

    assert version("urllib3-future") == urllib3_future.__version__ + "+isolation"
    assert not hasattr(urllib3, "AsyncPoolManager")
    assert niquests.packages.urllib3 is urllib3_future
    assert requests.packages.urllib3 is urllib3

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        for client in (requests, niquests):
            with client.Session() as session:
                session.trust_env = False
                response = session.get(
                    f"http://127.0.0.1:{server.server_port}/", timeout=5
                )
                assert response.content == b"ok"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    print("Requests and niquests coexist and both complete a local request")


if __name__ == "__main__":
    if sys.argv[1:] == ["--installed"]:
        installed_smoke()
    else:
        unittest.main()
