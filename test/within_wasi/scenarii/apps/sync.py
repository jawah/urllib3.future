from __future__ import annotations

import urllib3  # noqa: F401
from wit_world import exports  # type: ignore[import-not-found]

from ..cases.sync import SyncWasiTests
from ..unittest_runner import run_sync_case, selected_case


class Run(exports.Run):  # type: ignore[misc]
    def run(self) -> None:
        case_id = selected_case()
        run_sync_case(case_id, SyncWasiTests)
