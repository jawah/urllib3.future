from __future__ import annotations

from wit_world import exports  # type: ignore[import-not-found]

from ..unittest_runner import measure_imports, run_sync_case, selected_case

with measure_imports():
    import urllib3  # noqa: F401
    from ..cases.sync import SyncWasiTests


class Run(exports.Run):  # type: ignore[misc]
    def run(self) -> None:
        case_id = selected_case()
        run_sync_case(case_id, SyncWasiTests)
