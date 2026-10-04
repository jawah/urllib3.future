from __future__ import annotations

from wit_world import exports  # type: ignore[import-not-found]

from ..unittest_runner import measure_imports, run_async_case, selected_case

with measure_imports():
    import urllib3  # noqa: F401
    from ..cases.asyncio import AsyncWasiTests


class Run(exports.Run):  # type: ignore[misc]
    async def run(self) -> None:
        case_id = selected_case()
        await run_async_case(case_id, AsyncWasiTests)
