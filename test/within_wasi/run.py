from __future__ import annotations

import http.client
import os
import shutil
import site
import subprocess
import sys
import time
import unittest
from pathlib import Path

from .scenarii.cases.asyncio import AsyncWasiTests
from .scenarii.cases.sync import SyncWasiTests
from .scenarii.unittest_runner import case_ids
from .services import NativeServices


ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "build" / "wasi"
FIXTURES = BUILD / "fixtures"
COVERAGE = BUILD / "coverage"
LOGS = BUILD / "logs"
WIT = Path(os.environ.get("COMPONENTIZE_WIT", "/opt/componentize-py/wit"))


def start_process(name: str, command: list[str]) -> subprocess.Popen[bytes]:
    log = (LOGS / f"{name}.log").open("wb")
    process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    process._wasi_log = log  # type: ignore[attr-defined]
    return process


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    process._wasi_log.close()  # type: ignore[attr-defined]


def wait_for_http() -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            connection = http.client.HTTPConnection("127.0.0.1", 8888, timeout=1)
            connection.request("GET", "/get", headers={"Host": "httpbin.local"})
            response = connection.getresponse()
            response.read()
            connection.close()
            if response.status == 200:
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("Traefik/go-httpbin did not become ready")


def build_components() -> tuple[Path, Path, Path, Path, Path, Path, Path]:
    sync_component = BUILD / "urllib3-sync.wasm"
    async_component = BUILD / "urllib3-async.wasm"
    minimal_sync_component = BUILD / "urllib3-minimal-sync.wasm"
    minimal_async_component = BUILD / "urllib3-minimal-async.wasm"
    dual_component = BUILD / "urllib3-dual.wasm"
    p1_fallback_component = BUILD / "urllib3-p1-fallback.wasm"
    no_rtls_component = BUILD / "urllib3-no-rtls.wasm"
    common = [
        "componentize-py",
        "-d",
        str(WIT),
    ]
    python_paths = [
        "-p",
        str(ROOT / "test" / "within_wasi"),
        "-p",
        str(ROOT / "src"),
    ]

    subprocess.run(
        common
        + ["-w", "wasi:cli/command@0.2.0", "componentize"]
        + python_paths
        + ["scenarii.apps.sync", "-o", str(sync_component)],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        common
        + ["-w", "wasi:cli/command@0.3.0", "componentize"]
        + python_paths
        + ["scenarii.apps.asyncio", "-o", str(async_component)],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        common
        + ["-w", "wasi:cli/command@0.2.0", "componentize"]
        + python_paths
        + ["scenarii.apps.minimal_sync", "-o", str(minimal_sync_component)],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        common
        + ["-w", "wasi:cli/command@0.3.0", "componentize"]
        + python_paths
        + ["scenarii.apps.minimal_async", "-o", str(minimal_async_component)],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        [
            "componentize-py",
            "-d",
            str(WIT),
            "-d",
            str(ROOT / "test" / "within_wasi" / "wit" / "dual.wit"),
            "-w",
            "urllib3:wasi-tests/dual-command",
            "componentize",
        ]
        + python_paths
        + ["scenarii.apps.dual", "-o", str(dual_component)],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        [
            "componentize-py",
            "-d",
            str(WIT),
            "-d",
            str(ROOT / "test" / "within_wasi" / "wit" / "p1-fallback.wit"),
            "-w",
            "urllib3:p1-fallback/command",
            "componentize",
        ]
        + python_paths
        + ["scenarii.apps.p1_fallback", "-o", str(p1_fallback_component)],
        cwd=ROOT,
        check=True,
    )
    no_rtls_environment = os.environ.copy()
    no_rtls_venv = BUILD / "no-rtls-venv"
    no_rtls_site = (
        no_rtls_venv
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    shutil.rmtree(no_rtls_venv, ignore_errors=True)
    no_rtls_site.mkdir(parents=True)
    installed_site = Path(site.getsitepackages()[0])
    for dependency in installed_site.iterdir():
        normalized_name = dependency.name.lower().replace("-", "_")
        if normalized_name == "rtls" or normalized_name.startswith("rtls_"):
            continue
        destination = no_rtls_site / dependency.name
        if dependency.is_dir():
            shutil.copytree(dependency, destination)
        else:
            shutil.copy2(dependency, destination)
    no_rtls_environment["VIRTUAL_ENV"] = str(no_rtls_venv)
    subprocess.run(
        common
        + ["-w", "wasi:cli/command@0.2.0", "componentize"]
        + python_paths
        + ["scenarii.apps.no_rtls", "-o", str(no_rtls_component)],
        cwd=ROOT,
        env=no_rtls_environment,
        check=True,
    )
    return (
        sync_component,
        async_component,
        minimal_sync_component,
        minimal_async_component,
        dual_component,
        p1_fallback_component,
        no_rtls_component,
    )


def wasmtime_command(component: Path, case_id: str, *, async_: bool) -> list[str]:
    command = ["wasmtime", "run"]
    if async_:
        command.extend(["-Sp3", "-Wcomponent-model-async"])
    command.extend(
        [
            "-Sinherit-network",
            "-Sallow-ip-name-lookup=y",
            "--dir",
            f"{COVERAGE}::coverage",
            "--dir",
            f"{FIXTURES}::fixtures",
            str(component),
            case_id,
        ]
    )
    return command


def run_minimal_component(component: Path, *, async_: bool) -> None:
    command = ["wasmtime", "run"]
    if async_:
        command.extend(["-Sp3", "-Wcomponent-model-async"])
    command.extend(
        [
            "-Sinherit-network",
            "-Sallow-ip-name-lookup=y",
            "--dir",
            f"{FIXTURES}::fixtures",
            str(component),
        ]
    )
    subprocess.run(command, cwd=ROOT, check=True, timeout=30)


def run_dual_component(component: Path) -> None:
    for mode in ("sync", "async", "both"):
        subprocess.run(
            [
                "wasmtime",
                "run",
                "-Sp3",
                "-Wcomponent-model-async",
                "-Sinherit-network",
                "-Sallow-ip-name-lookup=y",
                "--dir",
                f"{FIXTURES}::fixtures",
                str(component),
                mode,
            ],
            cwd=ROOT,
            check=True,
            timeout=30,
        )


def run_cases(component: Path, cases: list[str], *, async_: bool) -> list[str]:
    failures = []
    kind = "async" if async_ else "sync"
    for case_id in cases:
        try:
            subprocess.run(
                wasmtime_command(component, case_id, async_=async_),
                cwd=ROOT,
                check=True,
                timeout=90,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            print(f"WASI {kind} case failed: {case_id}: {error}", file=sys.stderr)
            failures.append(f"{kind}:{case_id}")
    return failures


def selected_cases(kind: str, case_class: type[unittest.TestCase]) -> list[str]:
    available = case_ids(case_class)
    configured = os.environ.get(f"WASI_{kind.upper()}_CASES")
    if configured is None:
        return available
    selected = [case.strip() for case in configured.split(",") if case.strip()]
    unknown = sorted(set(selected).difference(available))
    if unknown:
        raise RuntimeError(f"unknown WASI {kind} cases: {', '.join(unknown)}")
    return selected


def combine_coverage() -> None:
    data_files = list(COVERAGE.glob(".coverage.wasi.*"))
    if not data_files:
        raise RuntimeError("WASI tests produced no coverage data")
    output = ROOT / ".coverage.wasi"
    output.unlink(missing_ok=True)
    environment = os.environ.copy()
    environment["COVERAGE_FILE"] = str(output)
    subprocess.run(
        [sys.executable, "-m", "coverage", "combine", "--keep", str(COVERAGE)],
        cwd=ROOT,
        env=environment,
        check=True,
    )


def main() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(COVERAGE, ignore_errors=True)
    COVERAGE.mkdir(parents=True)

    services = NativeServices(FIXTURES)
    hosts = Path("/etc/hosts")
    hosts_text = hosts.read_text(encoding="utf-8")
    if "httpbin.local" not in hosts_text:
        with hosts.open("a", encoding="utf-8") as file:
            file.write("\n127.0.0.1 httpbin.local alt.httpbin.local\n")
    processes = [
        start_process(
            "http-proxy", [sys.executable, "-m", "dummyserver.proxy", "18080"]
        ),
        start_process(
            "https-proxy", [sys.executable, "-m", "dummyserver.https_proxy", "18443"]
        ),
    ]

    try:
        wait_for_http()
        (
            sync_component,
            async_component,
            minimal_sync_component,
            minimal_async_component,
            dual_component,
            p1_fallback_component,
            no_rtls_component,
        ) = build_components()
        run_minimal_component(minimal_sync_component, async_=False)
        run_minimal_component(minimal_async_component, async_=True)
        run_minimal_component(p1_fallback_component, async_=False)
        run_minimal_component(no_rtls_component, async_=False)
        run_dual_component(dual_component)
        failures = run_cases(
            sync_component, selected_cases("sync", SyncWasiTests), async_=False
        )
        failures.extend(
            run_cases(
                async_component,
                selected_cases("async", AsyncWasiTests),
                async_=True,
            )
        )
        combine_coverage()
        if failures:
            raise SystemExit("WASI test failures: " + ", ".join(failures))
    finally:
        for process in reversed(processes):
            stop_process(process)
        services.close()


if __name__ == "__main__":
    main()
