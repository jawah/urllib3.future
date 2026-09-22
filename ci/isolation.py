"""Build cohabitation wheels and maintain their static Pages index."""

from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import quote
from urllib.request import urlopen
import zipfile

from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version


def wheel_metadata(wheel: Path) -> tuple[Version, str]:
    name, version, _, _ = parse_wheel_filename(wheel.name)
    if name != "urllib3-future" or version.local != "isolation":
        raise ValueError(f"Not an isolation wheel: {wheel.name}")
    with zipfile.ZipFile(wheel) as archive:
        dist_info = f"urllib3_future-{version}.dist-info"
        metadata = BytesParser().parsebytes(archive.read(f"{dist_info}/METADATA"))
        roots = {name.split("/", 1)[0] for name in archive.namelist()}
        if roots != {"urllib3_future", dist_info} or any(
            name.endswith(".pth") for name in archive.namelist()
        ):
            raise ValueError(
                "Isolation wheels must contain only urllib3_future and dist-info"
            )
        if (
            canonicalize_name(metadata["Name"]) != name
            or Version(metadata["Version"]) != version
        ):
            raise ValueError("Wheel filename and metadata disagree")
        embedded = archive.read("urllib3_future/_version.py").decode()
        if not re.search(
            rf'^__version__ = "{re.escape(version.public)}"$', embedded, re.M
        ):
            raise ValueError("Package and wheel versions disagree")
        requires_python = metadata["Requires-Python"]
        if not requires_python:
            raise ValueError("Missing Requires-Python")
    return version, requires_python


def immutable_copy(source: Path, destination: Path) -> None:
    if destination.exists():
        if source.read_bytes() != destination.read_bytes():
            raise ValueError(
                f"Refusing to replace published artifact: {destination.name}"
            )
    else:
        shutil.copyfile(source, destination)


def build(sdist: Path, output: Path) -> None:
    """Build from the release sdist without modifying the checkout."""
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="urllib3-isolation-") as temporary:
        with tarfile.open(sdist) as archive:
            archive.extractall(temporary, filter="data")
        (source,) = Path(temporary).iterdir()
        pkg_info = source / "PKG-INFO"
        metadata = BytesParser().parsebytes(pkg_info.read_bytes())
        version = Version(metadata["Version"])
        if version.local is not None:
            raise ValueError("Expected an unmodified public release sdist")
        isolation_version = f"{version}+isolation"
        # Hatchling reuses sdist metadata instead of recalculating its version.
        # Keep the runtime __version__ unchanged for downstream version checks.
        pkg_info.write_bytes(
            pkg_info.read_bytes().replace(
                f"Version: {version}\n".encode(),
                f"Version: {isolation_version}\n".encode(),
                1,
            )
        )
        environment = dict(os.environ, URLLIB3_NO_OVERRIDE="1")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--outdir",
                str(source / "wheel"),
            ],
            cwd=source,
            env=environment,
            check=True,
        )
        (wheel,) = (source / "wheel").glob("*.whl")
        wheel_metadata(wheel)
        with zipfile.ZipFile(wheel) as archive:
            result = BytesParser().parsebytes(
                archive.read(f"urllib3_future-{isolation_version}.dist-info/METADATA")
            )
        for field in ("Requires-Python", "Requires-Dist", "Provides-Extra"):
            if result.get_all(field) != metadata.get_all(field):
                raise ValueError(f"Isolation build changed {field}")
        immutable_copy(wheel, output / wheel.name)


def page(path: Path, title: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title></head><body>\n{body}\n</body></html>\n",
        encoding="utf-8",
    )


def restore(site: Path, releases: Path) -> None:
    """Restore wheels from release assets, undoing GitHub's filename sanitization."""
    project = site / "isolation/simple/urllib3-future"
    project.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="urllib3-isolation-assets-") as temporary:
        for asset in json.loads(releases.read_text()):
            name = asset["name"].replace(".isolation-", "+isolation-", 1)
            if (
                not name.startswith("urllib3_future-")
                or "+isolation-" not in name
                or not name.endswith(".whl")
            ):
                continue
            if Path(name).name != name:
                raise ValueError(f"Invalid release asset name: {name}")
            wheel = Path(temporary) / name
            with urlopen(asset["browser_download_url"], timeout=30) as response:
                with wheel.open("wb") as destination:
                    shutil.copyfileobj(response, destination)
            wheel_metadata(wheel)
            digest = asset.get("digest")
            if (
                digest
                and digest != "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest()
            ):
                raise ValueError(f"Release asset digest mismatch: {name}")
            immutable_copy(wheel, project / name)


def index(site: Path, wheel: Path) -> None:
    wheel_metadata(wheel)
    project = site / "isolation/simple/urllib3-future"
    project.mkdir(parents=True, exist_ok=True)
    immutable_copy(wheel, project / wheel.name)
    links = []
    rows = []
    for stored in sorted(
        project.glob("*.whl"), key=lambda p: wheel_metadata(p)[0], reverse=True
    ):
        version, requires_python = wheel_metadata(stored)
        digest = hashlib.sha256(stored.read_bytes()).hexdigest()
        filename = quote(stored.name)
        links.append(
            f'<a href="{filename}#sha256={digest}" '
            f'data-requires-python="{html.escape(requires_python, quote=True)}">'
            f"{html.escape(stored.name)}</a><br>"
        )
        prefix = "simple/urllib3-future/"
        provenance = (
            "https://github.com/jawah/urllib3.future/releases/download/"
            f"{version.public}/multiple.intoto.jsonl"
        )
        rows.append(
            f'<li><a href="{prefix}{filename}#sha256={digest}">{version}</a> '
            f'(<a href="{provenance}">Release SLSA provenance</a>)'
            f"<br><code>sha256:{digest}</code></li>"
        )
    page(project / "index.html", "urllib3-future isolation wheels", "\n".join(links))
    page(
        site / "isolation/simple/index.html",
        "Isolation index",
        '<a href="urllib3-future/">urllib3-future</a>',
    )
    page(
        site / "isolation/index.html",
        "urllib3.future cohabitation wheels",
        "<h1>urllib3.future cohabitation wheels</h1>"
        "<p>These wheels provide <code>urllib3_future</code> without replacing "
        "<code>urllib3</code> or installing a startup hook. Versions end in <code>+isolation</code>.</p>"
        "<p>For installation and attestation verification, see the "
        '<a href="https://urllib3future.readthedocs.io/en/latest/cohabitation.html">instructions</a>. '
        "Use an exact <code>+isolation</code> version or a package-specific index binding.</p>"
        "<ul>" + "\n".join(rows) + "</ul>",
    )
    page(
        site / "index.html",
        "urllib3.future wheels",
        '<a href="isolation/">Cohabitation wheels</a>',
    )
    (site / ".nojekyll").touch()


def check(base_url: str, wheel: Path) -> None:
    """Check the deployed bytes, allowing for Pages propagation."""
    project = base_url.rstrip("/") + "/isolation/simple/urllib3-future/"
    for attempt in range(30):
        try:
            with urlopen(project + quote(wheel.name), timeout=15) as response:
                if response.read() != wheel.read_bytes():
                    raise ValueError(f"Published bytes differ: {wheel.name}")
            return
        except (OSError, ValueError):
            if attempt == 29:
                raise
            time.sleep(10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    builder = commands.add_parser("build")
    builder.add_argument("sdist", type=Path)
    builder.add_argument("--output", type=Path, default=Path("dist-isolation"))
    restorer = commands.add_parser("restore")
    restorer.add_argument("site", type=Path)
    restorer.add_argument("releases", type=Path)
    publisher = commands.add_parser("index")
    publisher.add_argument("site", type=Path)
    publisher.add_argument("wheel", type=Path)
    checker = commands.add_parser("check")
    checker.add_argument("base_url")
    checker.add_argument("wheel", type=Path)
    args = parser.parse_args()
    if args.command == "build":
        build(args.sdist, args.output)
    elif args.command == "restore":
        restore(args.site, args.releases)
    elif args.command == "index":
        index(args.site, args.wheel)
    else:
        check(args.base_url, args.wheel)


if __name__ == "__main__":
    main()
