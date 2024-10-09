from __future__ import annotations

from test import requires_network

import pytest

from urllib3 import AsyncHTTPResponse, AsyncPoolManager, HTTPResponse, PoolManager

# Unfortunately gohttpbin does not support Informational Responses
# as of october 2024. We'll need to create a feature request at
# https://github.com/mccutchen/go-httpbin
# early-hints.fastlylabs.com does not support HTTP/3..!
# ...
# todo: migrate this test to with_traefik once gohttpbin support this!
#       those tests are incomplete and does NOT give enough guarantees.


@requires_network()
def test_sync_no_multiplex_early_response() -> None:
    early_response: HTTPResponse | None = None

    def dump_callback(prior_response: HTTPResponse) -> None:
        nonlocal early_response
        early_response = prior_response

    with PoolManager() as pm:
        resp = pm.urlopen(
            "GET",
            "https://early-hints.fastlylabs.com/",
            on_early_response=dump_callback,
        )

    assert early_response is not None
    assert early_response.status == 103
    assert early_response.headers
    assert early_response.data == b""
    assert "link" in early_response.headers

    assert resp.version == 20  # failsafe/guard in case the remote changes!

    assert resp.status == 200
    assert resp.headers
    assert "link" not in resp.headers


@requires_network()
def test_sync_with_multiplex_early_response() -> None:
    early_response: HTTPResponse | None = None

    def dump_callback(prior_response: HTTPResponse) -> None:
        nonlocal early_response
        early_response = prior_response

    with PoolManager() as pm:
        promise = pm.urlopen(
            "GET",
            "https://early-hints.fastlylabs.com/",
            on_early_response=dump_callback,
            multiplexed=True,
        )

        # cannot be received at this stage!
        assert early_response is None

        resp = pm.get_response(promise=promise)

    assert early_response is not None
    assert early_response.status == 103
    assert early_response.version == 20  # failsafe/guard in case the remote changes!
    assert early_response.headers
    assert "link" in early_response.headers

    payload: bytes = b""

    for chunk in early_response.stream():
        payload += chunk

    assert payload == b""

    assert resp.version == 20  # failsafe/guard in case the remote changes!

    assert resp.status == 200
    assert resp.headers
    assert "link" not in resp.headers


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
