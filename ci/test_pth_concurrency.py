from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
_SITE_PACKAGES: tuple[Path, ...] | None = None
WORKER_CODE = """
import importlib.util
from pathlib import Path
import urllib3
import urllib3_future

assert urllib3.__version__ == urllib3_future.__version__
assert Path(urllib3.__file__).parent.name == "urllib3"
assert Path(urllib3.exceptions.__file__).parent.name == "urllib3"
assert importlib.util.find_spec("urllib3.exceptions") is not None
"""


def run(*args: str) -> None:
    print("+", *args, flush=True)
    subprocess.run(args, check=True)


def remove(path: Path) -> None:
    for attempt in range(50):
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                path.unlink()
            return
        except PermissionError:
            if attempt == 49:
                raise
            time.sleep(0.01)


def site_packages() -> tuple[Path, ...]:
    global _SITE_PACKAGES
    if _SITE_PACKAGES is None:
        output = subprocess.check_output(
            [
                PYTHON,
                "-c",
                "import json, sysconfig; "
                "print(json.dumps([sysconfig.get_path('purelib'), "
                "sysconfig.get_path('platlib')]))",
            ],
            text=True,
        )
        _SITE_PACKAGES = tuple({Path(path).resolve() for path in json.loads(output)})
    return _SITE_PACKAGES


def clean() -> None:
    run(
        PYTHON,
        "-m",
        "pip",
        "uninstall",
        "--yes",
        "urllib3",
        "urllib3-future",
    )

    for root in site_packages():
        for name in ("urllib3", "urllib3_future", "urllib3_future.pth"):
            remove(root / name)
        for pattern in ("urllib3-*.dist-info", "urllib3_future-*.dist-info"):
            for path in root.glob(pattern):
                remove(path)

        leftovers = [
            path
            for pattern in (
                "urllib3",
                "urllib3_future",
                "urllib3_future.pth",
                "urllib3-*.dist-info",
                "urllib3_future-*.dist-info",
            )
            for path in root.glob(pattern)
        ]
        assert not leftovers, f"incomplete cleanup: {leftovers}"


def install(requirement: Path) -> None:
    run(
        PYTHON,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        str(requirement),
    )


def burst(workers: int, timeout: float) -> None:
    processes = [
        subprocess.Popen(
            [PYTHON, "-c", WORKER_CODE],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(workers)
    ]
    deadline = time.monotonic() + timeout
    results = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(
                timeout=max(deadline - time.monotonic(), 0.001)
            )
            results.append((stdout, stderr, process.returncode))
    except subprocess.TimeoutExpired:
        for process in processes:
            if process.poll() is None:
                process.kill()
        for process in processes:
            process.communicate()
        raise RuntimeError(
            f"concurrent startup exceeded the {timeout:g}s timeout"
        ) from None
    failures = [result for result in results if result[2]]
    if failures:
        for stdout, stderr, returncode in failures:
            print(f"worker exited with {returncode}", file=sys.stderr)
            print(stdout, file=sys.stderr)
            print(stderr, file=sys.stderr)
        raise RuntimeError(f"{len(failures)}/{workers} workers failed")


def active_root() -> Path:
    matches = [root for root in site_packages() if (root / "urllib3_future").is_dir()]
    assert len(matches) == 1, f"expected one active site-packages root, got {matches}"
    return matches[0]


def verify() -> None:
    root = active_root()
    source = root / "urllib3_future"
    destination = root / "urllib3"

    assert (destination / ".u3f_sync").read_bytes() == (
        source / "_version.py"
    ).read_bytes()
    assert not list(destination.rglob("*.tmp"))

    mismatches = []
    for source_path in source.rglob("*"):
        if (
            not source_path.is_file()
            or "__pycache__" in source_path.parts
            or source_path.suffix in {".pyc", ".pyo"}
        ):
            continue
        destination_path = destination / source_path.relative_to(source)
        if (
            not destination_path.is_file()
            or source_path.read_bytes() != destination_path.read_bytes()
        ):
            mismatches.append(source_path.relative_to(source))
    assert not mismatches, f"source/destination mismatches: {mismatches}"


def build_wheel(directory: Path) -> Path:
    run(
        PYTHON,
        "-m",
        "pip",
        "wheel",
        "--disable-pip-version-check",
        "--no-deps",
        "--wheel-dir",
        str(directory),
        str(PROJECT_ROOT),
    )
    wheels = list(directory.glob("urllib3_future-*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"
    return wheels[0]


def download_upstream_wheel(directory: Path) -> Path:
    run(
        PYTHON,
        "-m",
        "pip",
        "download",
        "--disable-pip-version-check",
        "--no-deps",
        "--dest",
        str(directory),
        "urllib3",
    )
    wheels = list(directory.glob("urllib3-*.whl"))
    assert len(wheels) == 1, f"expected one upstream wheel, got {wheels}"
    return wheels[0]


def exercise(
    label: str,
    requirements: tuple[Path, Path],
    workers: int,
    timeout: float,
) -> None:
    print(f"\n--- {label} ---", flush=True)
    clean()
    for requirement in requirements:
        install(requirement)
    started = time.monotonic()
    burst(workers, timeout)
    verify()
    print(f"{label}: {workers} workers passed in {time.monotonic() - started:.2f}s")


def main() -> None:
    if "site" in sys.modules:
        raise RuntimeError("run this harness with python -S")

    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--wheel", type=Path)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="urllib3-future-wheel-") as tmp:
        directory = Path(tmp)
        future_wheel = args.wheel.resolve() if args.wheel else build_wheel(directory)
        upstream_wheel = download_upstream_wheel(directory)
        assert future_wheel.is_file(), f"wheel does not exist: {future_wheel}"
        for round_number in range(1, args.rounds + 1):
            exercise(
                f"round {round_number}: upstream first",
                (upstream_wheel, future_wheel),
                args.workers,
                args.timeout,
            )
            exercise(
                f"round {round_number}: urllib3.future first",
                (future_wheel, upstream_wheel),
                args.workers,
                args.timeout,
            )


if __name__ == "__main__":
    main()
