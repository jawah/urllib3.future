from __future__ import annotations

import typing

from test import requires_network

import pytest

from urllib3 import (
    AsyncPoolManager,
    ConnectionInfo,
    HttpVersion,
    PoolManager,
)
from urllib3.backend._async.hface import _HAS_HTTP3_SUPPORT as _ASYNC_HAS_HTTP3_SUPPORT  # type: ignore[attr-defined]
from urllib3.backend.hface import _HAS_HTTP3_SUPPORT as _SYNC_HAS_HTTP3_SUPPORT


@requires_network()
@pytest.mark.parametrize(
    "happy_eyeballs",
    [False, True],
    ids=["heb_off", "heb_on"],
)
@pytest.mark.parametrize(
    "http_version",
    [11, 20, 30],
    ids=["h1", "h2", "h3"],
)
def test_sync_ech_accepted(happy_eyeballs: bool, http_version: int) -> None:
    if http_version == 30 and not _SYNC_HAS_HTTP3_SUPPORT():
        pytest.skip("Test requires HTTP/3 support")
    if http_version != 30:
        try:
            import rtls
        except ImportError:
            pytest.skip("Test requires rtls for ECH at TCP level")

    disabled_svn: set[HttpVersion] = set()

    if http_version == 11:
        disabled_svn.add(HttpVersion.h3)
        disabled_svn.add(HttpVersion.h2)
    elif http_version == 20:
        disabled_svn.add(HttpVersion.h11)
        disabled_svn.add(HttpVersion.h3)
    elif http_version == 30:
        disabled_svn.add(HttpVersion.h11)
        disabled_svn.add(HttpVersion.h2)

    conn_info_ref: ConnectionInfo | None = None

    def on_post_connection(conn_info: ConnectionInfo) -> None:
        nonlocal conn_info_ref
        conn_info_ref = conn_info

    pm_kwargs: dict[str, typing.Any] = {
        "resolver": "doh+google://",
        "disabled_svn": disabled_svn,
        "happy_eyeballs": happy_eyeballs,
    }

    with PoolManager(**pm_kwargs) as pm:
        resp = pm.urlopen(
            "GET",
            "https://encryptedsni.com/",
            redirect=False,
            on_post_connection=on_post_connection,
        )

    assert conn_info_ref is not None
    assert conn_info_ref.tls_ech_accepted is True


@requires_network()
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "happy_eyeballs",
    [False, True],
    ids=["heb_off", "heb_on"],
)
@pytest.mark.parametrize(
    "http_version",
    [11, 20, 30],
    ids=["h1", "h2", "h3"],
)
async def test_async_ech_accepted(happy_eyeballs: bool, http_version: int) -> None:
    if http_version == 30 and not _ASYNC_HAS_HTTP3_SUPPORT():
        pytest.skip("Test requires HTTP/3 support")
    if http_version != 30:
        try:
            import rtls
        except ImportError:
            pytest.skip("Test requires rtls for ECH at TCP level")

    disabled_svn: set[HttpVersion] = set()

    if http_version == 11:
        disabled_svn.add(HttpVersion.h3)
        disabled_svn.add(HttpVersion.h2)
    elif http_version == 20:
        disabled_svn.add(HttpVersion.h11)
        disabled_svn.add(HttpVersion.h3)
    elif http_version == 30:
        disabled_svn.add(HttpVersion.h11)
        disabled_svn.add(HttpVersion.h2)

    conn_info_ref: ConnectionInfo | None = None

    async def on_post_connection(conn_info: ConnectionInfo) -> None:
        nonlocal conn_info_ref
        conn_info_ref = conn_info

    pm_kwargs: dict[str, typing.Any] = {
        "resolver": "doh+google://",
        "disabled_svn": disabled_svn,
        "happy_eyeballs": happy_eyeballs,
    }

    async with AsyncPoolManager(**pm_kwargs) as pm:
        await pm.urlopen(
            "GET",
            "https://encryptedsni.com/",
            redirect=False,
            on_post_connection=on_post_connection,
        )

    assert conn_info_ref is not None
    assert conn_info_ref.tls_ech_accepted is True
