from __future__ import annotations

import pytest

from urllib3 import AsyncPoolManager
from urllib3._async.connectionpool import log as cp_logger
from urllib3.exceptions import InsecureRequestWarning, NewConnectionError

from ... import LogRecorder
from ...conftest import ServerConfig


@pytest.mark.asyncio
class TestHappyEyeballsOverHTTPS:
    async def test_dual_stack(self, ipv6_san_server: ServerConfig) -> None:
        async with AsyncPoolManager(
            happy_eyeballs=True,
            resolver="in-memory://default?hosts=dummy.io:127.0.0.1,dummy.io:[::1]",
            cert_reqs=0,
        ) as pm:
            with LogRecorder(target=cp_logger) as logs:
                with pytest.warns(InsecureRequestWarning):
                    r = await pm.urlopen(
                        "GET", f"https://dummy.io:{ipv6_san_server.port}/"
                    )

            attempt_happy_eyeballs: bool = False
            choose_dual_stack: bool = False
            ineligible_records: bool = False

            for log in logs:
                if (
                    not attempt_happy_eyeballs
                    and "Attempting Happy-Eyeball" in log.message
                ):
                    attempt_happy_eyeballs = True
                    continue
                if not choose_dual_stack and "Happy-Eyeball Dual-Stack" in log.message:
                    choose_dual_stack = True
                if not ineligible_records and "Happy-Eyeball Ineligible" in log.message:
                    ineligible_records = True

            assert attempt_happy_eyeballs
            assert choose_dual_stack
            assert not ineligible_records
            assert r.status == 200

    async def test_single_stack(self, ipv6_san_server: ServerConfig) -> None:
        async with AsyncPoolManager(
            happy_eyeballs=True,
            resolver="in-memory://default?hosts=dummy.io:[::2],dummy.io:[::1]",
            cert_reqs=0,
        ) as pm:
            with LogRecorder(target=cp_logger) as logs:
                with pytest.warns(InsecureRequestWarning):
                    r = await pm.urlopen(
                        "GET", f"https://dummy.io:{ipv6_san_server.port}/"
                    )

            attempt_happy_eyeballs: bool = False
            choose_single_stack: bool = False
            ineligible_records: bool = False

            for log in logs:
                if (
                    not attempt_happy_eyeballs
                    and "Attempting Happy-Eyeball" in log.message
                ):
                    attempt_happy_eyeballs = True
                    continue
                if (
                    not choose_single_stack
                    and "Happy-Eyeball Single-Stack" in log.message
                ):
                    choose_single_stack = True
                    continue
                if not ineligible_records and "Happy-Eyeball Ineligible" in log.message:
                    ineligible_records = True

            assert attempt_happy_eyeballs
            assert choose_single_stack
            assert not ineligible_records
            assert r.status == 200

    async def test_with_one_tarpit(self, ipv6_san_server: ServerConfig) -> None:
        async with AsyncPoolManager(
            happy_eyeballs=True,
            resolver="in-memory://default?hosts=dummy.io:240.0.0.0,dummy.io:[::1]",
            cert_reqs=0,
        ) as pm:
            with LogRecorder(target=cp_logger) as logs:
                with pytest.warns(InsecureRequestWarning):
                    r = await pm.urlopen(
                        "GET", f"https://dummy.io:{ipv6_san_server.port}/"
                    )

            attempt_happy_eyeballs: bool = False
            choose_dual_stack: bool = False
            ineligible_records: bool = False

            for log in logs:
                if (
                    not attempt_happy_eyeballs
                    and "Attempting Happy-Eyeball" in log.message
                ):
                    attempt_happy_eyeballs = True
                    continue
                if not choose_dual_stack and "Happy-Eyeball Dual-Stack" in log.message:
                    choose_dual_stack = True
                    continue
                if not ineligible_records and "Happy-Eyeball Ineligible" in log.message:
                    ineligible_records = True

            assert attempt_happy_eyeballs
            assert choose_dual_stack
            assert not ineligible_records
            assert r.status == 200

    async def test_with_all_tarpit_global_timeout(self) -> None:
        async with AsyncPoolManager(
            happy_eyeballs=True,
            resolver="in-memory://default?hosts=dummy.io:240.0.0.0,dummy.io:240.0.0.1",
            cert_reqs=0,
            retries=False,
            timeout=0.5,
        ) as pm:
            with pytest.raises(NewConnectionError) as exc:
                await pm.urlopen("GET", "https://dummy.io/")

            assert (
                "No suitable address to connect to using Happy Eyeballs"
                in exc.value.args[0]
            )
            assert "within 0.5s" in exc.value.args[0]

    async def test_with_all_tarpit_local_timeout(self) -> None:
        async with AsyncPoolManager(
            happy_eyeballs=True,
            resolver="in-memory://default?hosts=dummy.io:240.0.0.0,dummy.io:240.0.0.1",
            cert_reqs=0,
            retries=False,
        ) as pm:
            with pytest.raises(NewConnectionError) as exc:
                await pm.urlopen("GET", "https://dummy.io/", timeout=0.5)

            assert (
                "No suitable address to connect to using Happy Eyeballs"
                in exc.value.args[0]
            )
            assert "within 0.5s" in exc.value.args[0]
            # we must ensure we can tie the NewConnectionError to a TimeoutError
            # some people relies on it.
            assert exc.value.__cause__ is not None

    async def test_ineligible_target(self, ipv6_san_server: ServerConfig) -> None:
        async with AsyncPoolManager(
            happy_eyeballs=True,
            resolver="in-memory://default?hosts=dummy.io:[::1]",
            cert_reqs=0,
        ) as pm:
            with LogRecorder(target=cp_logger) as logs:
                with pytest.warns(InsecureRequestWarning):
                    r = await pm.urlopen(
                        "GET", f"https://dummy.io:{ipv6_san_server.port}/"
                    )

            attempt_happy_eyeballs: bool = False
            choose_dual_stack: bool = False
            ineligible_records: bool = False

            for log in logs:
                if (
                    not attempt_happy_eyeballs
                    and "Attempting Happy-Eyeball" in log.message
                ):
                    attempt_happy_eyeballs = True
                    continue
                if not choose_dual_stack and "Happy-Eyeball Dual-Stack" in log.message:
                    choose_dual_stack = True
                    continue
                if not ineligible_records and "Happy-Eyeball Ineligible" in log.message:
                    ineligible_records = True

            assert attempt_happy_eyeballs
            assert not choose_dual_stack
            assert ineligible_records
            assert r.status == 200


@pytest.mark.asyncio
class TestHappyEyeballsOverHTTP:
    async def test_dual_stack(self, ipv6_plain_server: ServerConfig) -> None:
        async with AsyncPoolManager(
            happy_eyeballs=True,
            resolver="in-memory://default?hosts=dummy.io:127.0.0.1,dummy.io:[::1]",
            cert_reqs=0,
        ) as pm:
            with LogRecorder(target=cp_logger) as logs:
                r = await pm.urlopen(
                    "GET", f"http://dummy.io:{ipv6_plain_server.port}/"
                )

            attempt_happy_eyeballs: bool = False
            choose_dual_stack: bool = False
            ineligible_records: bool = False

            for log in logs:
                if (
                    not attempt_happy_eyeballs
                    and "Attempting Happy-Eyeball" in log.message
                ):
                    attempt_happy_eyeballs = True
                    continue
                if not choose_dual_stack and "Happy-Eyeball Dual-Stack" in log.message:
                    choose_dual_stack = True
                if not ineligible_records and "Happy-Eyeball Ineligible" in log.message:
                    ineligible_records = True

            assert attempt_happy_eyeballs
            assert choose_dual_stack
            assert not ineligible_records
            assert r.status == 200

    async def test_single_stack(self, ipv6_plain_server: ServerConfig) -> None:
        async with AsyncPoolManager(
            happy_eyeballs=True,
            resolver="in-memory://default?hosts=dummy.io:[::2],dummy.io:[::1]",
            cert_reqs=0,
        ) as pm:
            with LogRecorder(target=cp_logger) as logs:
                r = await pm.urlopen(
                    "GET", f"http://dummy.io:{ipv6_plain_server.port}/"
                )

            attempt_happy_eyeballs: bool = False
            choose_single_stack: bool = False
            ineligible_records: bool = False

            for log in logs:
                if (
                    not attempt_happy_eyeballs
                    and "Attempting Happy-Eyeball" in log.message
                ):
                    attempt_happy_eyeballs = True
                    continue
                if (
                    not choose_single_stack
                    and "Happy-Eyeball Single-Stack" in log.message
                ):
                    choose_single_stack = True
                    continue
                if not ineligible_records and "Happy-Eyeball Ineligible" in log.message:
                    ineligible_records = True

            assert attempt_happy_eyeballs
            assert choose_single_stack
            assert not ineligible_records
            assert r.status == 200

    async def test_with_one_tarpit(self, ipv6_plain_server: ServerConfig) -> None:
        async with AsyncPoolManager(
            happy_eyeballs=True,
            resolver="in-memory://default?hosts=dummy.io:240.0.0.0,dummy.io:[::1]",
            cert_reqs=0,
        ) as pm:
            with LogRecorder(target=cp_logger) as logs:
                r = await pm.urlopen(
                    "GET", f"http://dummy.io:{ipv6_plain_server.port}/"
                )

            attempt_happy_eyeballs: bool = False
            choose_dual_stack: bool = False
            ineligible_records: bool = False

            for log in logs:
                if (
                    not attempt_happy_eyeballs
                    and "Attempting Happy-Eyeball" in log.message
                ):
                    attempt_happy_eyeballs = True
                    continue
                if not choose_dual_stack and "Happy-Eyeball Dual-Stack" in log.message:
                    choose_dual_stack = True
                    continue
                if not ineligible_records and "Happy-Eyeball Ineligible" in log.message:
                    ineligible_records = True

            assert attempt_happy_eyeballs
            assert choose_dual_stack
            assert not ineligible_records
            assert r.status == 200

    async def test_with_all_tarpit_global_timeout(self) -> None:
        async with AsyncPoolManager(
            happy_eyeballs=True,
            resolver="in-memory://default?hosts=dummy.io:240.0.0.0,dummy.io:240.0.0.1",
            cert_reqs=0,
            retries=False,
            timeout=0.5,
        ) as pm:
            with pytest.raises(NewConnectionError) as exc:
                await pm.urlopen("GET", "http://dummy.io/")

            assert (
                "No suitable address to connect to using Happy Eyeballs"
                in exc.value.args[0]
            )
            assert "within 0.5s" in exc.value.args[0]

    async def test_with_all_tarpit_local_timeout(self) -> None:
        async with AsyncPoolManager(
            happy_eyeballs=True,
            resolver="in-memory://default?hosts=dummy.io:240.0.0.0,dummy.io:240.0.0.1",
            cert_reqs=0,
            retries=False,
        ) as pm:
            with pytest.raises(NewConnectionError) as exc:
                await pm.urlopen("GET", "http://dummy.io/", timeout=0.5)

            assert (
                "No suitable address to connect to using Happy Eyeballs"
                in exc.value.args[0]
            )
            assert "within 0.5s" in exc.value.args[0]
            # we must ensure we can tie the NewConnectionError to a TimeoutError
            # some people relies on it.
            assert exc.value.__cause__ is not None

    async def test_ineligible_target(self, ipv6_plain_server: ServerConfig) -> None:
        async with AsyncPoolManager(
            happy_eyeballs=True,
            resolver="in-memory://default?hosts=dummy.io:[::1]",
            cert_reqs=0,
        ) as pm:
            with LogRecorder(target=cp_logger) as logs:
                r = await pm.urlopen(
                    "GET", f"http://dummy.io:{ipv6_plain_server.port}/"
                )

            attempt_happy_eyeballs: bool = False
            choose_dual_stack: bool = False
            ineligible_records: bool = False

            for log in logs:
                if (
                    not attempt_happy_eyeballs
                    and "Attempting Happy-Eyeball" in log.message
                ):
                    attempt_happy_eyeballs = True
                    continue
                if not choose_dual_stack and "Happy-Eyeball Dual-Stack" in log.message:
                    choose_dual_stack = True
                    continue
                if not ineligible_records and "Happy-Eyeball Ineligible" in log.message:
                    ineligible_records = True

            assert attempt_happy_eyeballs
            assert not choose_dual_stack
            assert ineligible_records
            assert r.status == 200
