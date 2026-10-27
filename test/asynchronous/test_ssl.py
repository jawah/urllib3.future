from __future__ import annotations

import typing

import pytest
import trustme

from urllib3.exceptions import SSLError


@pytest.mark.asyncio
async def test_async_tls_missing_ca_file(tmp_path: typing.Any) -> None:
    from urllib3.contrib.ssa import AsyncSocket
    from urllib3.util._async.ssl_ import ssl_wrap_socket

    sock = AsyncSocket()
    try:
        with pytest.raises(SSLError) as caught:
            await ssl_wrap_socket(sock, ca_certs=str(tmp_path / "missing.pem"))
        assert isinstance(caught.value.__cause__, OSError)
    finally:
        sock.close()


@pytest.mark.asyncio
async def test_async_in_memory_certificate_unavailable_warns(
    san_server: typing.Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib3 import AsyncPoolManager
    from urllib3.contrib import imcc
    from urllib3.contrib.anytls import IS_NONSTDLIB

    if IS_NONSTDLIB:
        pytest.skip("Alternative backends support in-memory certificates natively")
    client = trustme.CA().issue_cert("client.example")
    monkeypatch.setattr(imcc, "SUPPORTED_METHODS", [])
    async with AsyncPoolManager(
        ca_certs=san_server.ca_certs,
        cert_data=client.cert_chain_pems[0].bytes(),
        key_data=client.private_key_pem.bytes(),
        timeout=5,
        retries=False,
    ) as manager:
        with pytest.warns(UserWarning, match="in-memory.*unsupported on your platform"):
            response = await manager.request("GET", san_server.base_url)
        assert response.status == 200
