from __future__ import annotations

import urllib3
from wit_world import exports  # type: ignore[import-not-found]


class Run(exports.Run):  # type: ignore[misc]
    def run(self) -> None:
        with urllib3.PoolManager(ca_certs="fixtures/root-ca.pem") as pool:
            response = pool.urlopen("GET", "https://httpbin.local:4443/get")
            assert response.status == 200
            assert response.version == 20
