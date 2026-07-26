from __future__ import annotations

import inspect
import sqlite3  # noqa: F401
import sys
import traceback
import unittest

from coverage import Coverage


def case_ids(case_class: type[unittest.TestCase]) -> list[str]:
    return [
        method.removeprefix("test_").replace("_", "-")
        for method in unittest.defaultTestLoader.getTestCaseNames(case_class)
    ]


def _method_name(case_id: str, case_class: type[unittest.TestCase]) -> str:
    method = "test_" + case_id.replace("-", "_")
    if method not in unittest.defaultTestLoader.getTestCaseNames(case_class):
        raise RuntimeError(
            f"unknown WASI case {case_id!r}; available: "
            f"{', '.join(case_ids(case_class))}"
        )
    return method


def _coverage(kind: str, case_id: str) -> Coverage:
    safe_case_id = case_id.replace("/", "_").replace(":", "_")
    coverage = Coverage(
        include=["*/urllib3/*"],
        check_preimported=True,
        config_file=False,
        data_file=f"coverage/.coverage.wasi.{kind}.{safe_case_id}",
        timid=True,
    )
    coverage.set_option("run:disable_warnings", ["already-imported"])
    return coverage


def run_sync_case(case_id: str, case_class: type[unittest.TestCase]) -> None:
    method_name = _method_name(case_id, case_class)
    coverage = _coverage("sync", case_id)
    coverage.start()
    try:
        suite = unittest.TestSuite([case_class(method_name)])
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        if not result.wasSuccessful():
            raise RuntimeError(f"WASI sync test failed: {case_id}")
    finally:
        coverage.stop()
        coverage.save()


async def run_async_case(case_id: str, case_class: type[unittest.TestCase]) -> None:
    method_name = _method_name(case_id, case_class)
    case = case_class(method_name)
    coverage = _coverage("async", case_id)
    coverage.start()
    print(f"{case_id} ({case.id()}) ... ", end="")

    try:
        case_class.setUpClass()
        case.setUp()
        async_setup = getattr(case, "asyncSetUp", None)
        if async_setup is not None:
            await async_setup()

        method = getattr(case, method_name)
        value = method()
        if not inspect.isawaitable(value):
            raise TypeError(f"async WASI test did not return an awaitable: {case.id()}")
        await value

        async_teardown = getattr(case, "asyncTearDown", None)
        if async_teardown is not None:
            await async_teardown()
        case.tearDown()
        case.doCleanups()
        case_class.tearDownClass()
    except BaseException:
        print("ERROR")
        traceback.print_exc()
        raise
    else:
        print("ok")
    finally:
        coverage.stop()
        coverage.save()


def selected_case() -> str:
    if len(sys.argv) != 2:
        raise RuntimeError(f"usage: {sys.argv[0]} <case-id>")
    return sys.argv[1]
