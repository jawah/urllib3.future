from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from urllib3 import PoolManager, HttpVersion, HTTPResponse
from urllib3.backend.hface import _HAS_HTTP3_SUPPORT

from . import TraefikTestCase
from .. import onlyCPython


class TestThreadSafety(TraefikTestCase):
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
                    responses.append(s.urlopen("GET", f"{self.https_url}/get"))
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
