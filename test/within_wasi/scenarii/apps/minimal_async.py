from __future__ import annotations

import urllib3
from wit_world import exports  # type: ignore[import-not-found]


class Run(exports.Run):  # type: ignore[misc]
    async def run(self) -> None:
        async with urllib3.AsyncPoolManager(ca_certs="fixtures/root-ca.pem") as pool:
            response = await pool.urlopen("GET", "https://httpbin.local:4443/get")
            assert response.status == 200
            assert response.version == 20
            await response.data
