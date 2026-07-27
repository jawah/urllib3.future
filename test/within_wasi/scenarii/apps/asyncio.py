from __future__ import annotations

import urllib3  # noqa: F401
from wit_world import exports  # type: ignore[import-not-found]

from ..cases.asyncio import AsyncWasiTests
from ..unittest_runner import run_async_case, selected_case


class Run(exports.Run):  # type: ignore[misc]
    async def run(self) -> None:
        case_id = selected_case()
        await run_async_case(case_id, AsyncWasiTests)
