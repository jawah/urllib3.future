from __future__ import annotations

import sys

import urllib3
from wit_world import exports  # type: ignore[import-not-found]


URL = "https://httpbin.local:4443/get"
CA_CERTS = "fixtures/root-ca.pem"


def run_sync() -> None:
    with urllib3.PoolManager(ca_certs=CA_CERTS) as pool:
        response = pool.urlopen("GET", URL)
        assert response.status == 200
        assert response.version == 20


async def run_async() -> None:
    async with urllib3.AsyncPoolManager(ca_certs=CA_CERTS) as pool:
        response = await pool.urlopen("GET", URL)
        assert response.status == 200
        assert response.version == 20
        await response.data


class Run(exports.Run):  # type: ignore[misc]
    async def run(self) -> None:
        if len(sys.argv) != 2 or sys.argv[1] not in {"sync", "async", "both"}:
            raise RuntimeError(f"usage: {sys.argv[0]} <sync|async|both>")

        mode = sys.argv[1]
        if mode in {"sync", "both"}:
            run_sync()
        if mode in {"async", "both"}:
            await run_async()

        print(f"dual P2/P3 component completed {mode} mode")
