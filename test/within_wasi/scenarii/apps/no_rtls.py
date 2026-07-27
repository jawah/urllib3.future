from __future__ import annotations

import importlib.util

import urllib3
from wit_world import exports  # type: ignore[import-not-found]


class Run(exports.Run):  # type: ignore[misc]
    def run(self) -> None:
        assert importlib.util.find_spec("rtls") is None

        with urllib3.PoolManager() as pool:
            response = pool.urlopen("GET", "http://httpbin.local:8888/get")
            assert response.status == 200
            assert response.version == 11

            try:
                pool.urlopen("GET", "https://httpbin.local:4443/get")
            except ImportError as error:
                assert str(error) == (
                    "Can't connect to HTTPS URL because the SSL module is not "
                    "available."
                )
            else:
                raise AssertionError("HTTPS must require an installed TLS backend")

        print("Plain HTTP works without rtls and HTTPS reports missing TLS support")
