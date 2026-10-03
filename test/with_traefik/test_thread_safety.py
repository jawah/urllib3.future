from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest

from urllib3 import (
    HTTPSConnectionPool,
    PoolManager,
    HttpVersion,
    HTTPResponse,
    ResponsePromise,
)
from urllib3.backend.hface import _HAS_HTTP3_SUPPORT

from . import TraefikTestCase
from .. import onlyCPython


class TestThreadSafety(TraefikTestCase):
    @pytest.mark.parametrize("svn_target", [HttpVersion.h2, HttpVersion.h3])
    @pytest.mark.parametrize("manager", [False, True])
    def test_concurrent_multiplexed_response_readers(
        self, svn_target: HttpVersion, manager: bool
    ) -> None:
        if svn_target is HttpVersion.h3 and not _HAS_HTTP3_SUPPORT():
            pytest.skip("Test requires http3 support")

        target = f"{self.https_url}/delay/0.1" if manager else "/delay/0.1"
        ready = Barrier(4)
        pool_kwargs: dict[str, Any] = {
            "ca_certs": self.ca_authority,
            "resolver": self.test_resolver.new(),
            "disabled_svn": {HttpVersion.h11, HttpVersion.h2, HttpVersion.h3}
            - {svn_target},
            "maxsize": 1,
            "timeout": 5,
        }
        with (
            PoolManager(**pool_kwargs)
            if manager
            else HTTPSConnectionPool(self.host, self.https_port, **pool_kwargs)
        ) as pool:
            assert pool.urlopen("GET", target).version == (
                20 if svn_target is HttpVersion.h2 else 30
            )

            def read() -> HTTPResponse | None:
                ready.wait(timeout=5)
                return pool.get_response()

            with ThreadPoolExecutor(max_workers=4) as workers:
                for _ in range(8):
                    for _ in range(4):
                        assert isinstance(
                            pool.urlopen("GET", target, multiplexed=True),
                            ResponsePromise,
                        )
                    # Propagate worker errors instead of losing them in raw threads.
                    futures = [workers.submit(read) for _ in range(4)]
                    responses = [future.result(timeout=10) for future in futures]
                    assert len({id(response) for response in responses}) == 4
                    assert all(
                        response is not None and response.status == 200
                        for response in responses
                    )
            assert pool.get_response() is None

    @onlyCPython()
    @pytest.mark.parametrize(
        "svn_target",
        [
            HttpVersion.h11,
            HttpVersion.h2,
            HttpVersion.h3,
        ],
    )
    @pytest.mark.parametrize(
        "pool_count",
        [
            1,
            2,
            3,
        ],
    )
    @pytest.mark.parametrize(
        "conn_maxsize",
        [
            1,
            2,
            10,
        ],
    )
    @pytest.mark.parametrize(
        "worker_maxsize",
        [
            2,
            8,
        ],
    )
    def test_pressure_traffic_police_scenario(
        self,
        svn_target: HttpVersion,
        pool_count: int,
        conn_maxsize: int,
        worker_maxsize: int,
    ) -> None:
        """
        This test is defined to challenge the thread safety of our pooling solution. If the suite execute itself without
        error, you can be confident that the safety isn't broken. In a GIL-enabled environment, this won't bring any
        confidence. Always run that test under the free threaded build.
        Symptoms of failures:
            - Hangs
            - SIG SEGFAULT
            - SIG ABRT
            - (stderr from libc about mem corruption)
            - Responses are not all there
            - At least one response is not HTTP 200 OK
        """

        if svn_target is HttpVersion.h3 and _HAS_HTTP3_SUPPORT() is False:
            pytest.skip("Test requires http3 support")

        def fetch_sixteen(s: PoolManager) -> list[HTTPResponse]:
            responses = []
            for _ in range(16):
                try:
                    responses.append(
                        s.urlopen("GET", f"{self.https_url}/get", timeout=10.0)
                    )
                except Exception as e:
                    print(e)
                    assert False
            return responses

        disabled_svn = {
            HttpVersion.h11,
            HttpVersion.h2,
            HttpVersion.h3,
        }

        disabled_svn.remove(svn_target)

        with PoolManager(
            pool_count,
            disabled_svn=disabled_svn,
            maxsize=conn_maxsize,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver.new(),
        ) as pm:
            with ThreadPoolExecutor(max_workers=worker_maxsize) as tpe:
                tasks = []

                for _ in range(worker_maxsize):
                    tasks.append(tpe.submit(fetch_sixteen, pm))

                for task in tasks:
                    responses = task.result()

                    assert len(responses) == 16
                    assert all(r.status for r in responses)
