from __future__ import annotations

import pytest

from test import requires_network
from urllib3 import AsyncHTTPResponse, AsyncPoolManager


@requires_network()
@pytest.mark.asyncio
async def test_async_no_multiplex_early_response() -> None:
    early_response: AsyncHTTPResponse | None = None

    async def dump_callback(prior_response: AsyncHTTPResponse) -> None:
        nonlocal early_response
        early_response = prior_response

    async with AsyncPoolManager() as pm:
        resp = await pm.urlopen(
            "GET",
            "https://early-hints.fastlylabs.com/",
            on_early_response=dump_callback,
        )

    assert early_response is not None
    assert early_response.status == 103
    assert early_response.headers
    assert (await early_response.read()) == b""
    assert "link" in early_response.headers

    assert resp.version == 20  # failsafe/guard in case the remote changes!

    assert resp.status == 200
    assert resp.headers
    assert "link" not in resp.headers


@requires_network()
@pytest.mark.asyncio
async def test_async_with_multiplex_early_response() -> None:
    early_response: AsyncHTTPResponse | None = None

    async def dump_callback(prior_response: AsyncHTTPResponse) -> None:
        nonlocal early_response
        early_response = prior_response

    async with AsyncPoolManager() as pm:
        promise = await pm.urlopen(
            "GET",
            "https://early-hints.fastlylabs.com/",
            on_early_response=dump_callback,
            multiplexed=True,
        )

        # cannot be received at this stage!
        assert early_response is None

        resp = await pm.get_response(promise=promise)

    assert early_response is not None
    assert early_response.status == 103
    assert early_response.version == 20  # failsafe/guard in case the remote changes!
    assert early_response.headers
    assert (await early_response.data) == b""
    assert "link" in early_response.headers

    assert resp.version == 20  # failsafe/guard in case the remote changes!

    assert resp.status == 200
    assert resp.headers
    assert "link" not in resp.headers
