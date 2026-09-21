from __future__ import annotations

import io
import typing

import pytest

from urllib3 import AsyncPoolManager, HTTPHeaderDict, PoolManager, Retry
from urllib3._async.connectionpool import AsyncHTTPConnectionPool
from urllib3._async.response import AsyncHTTPResponse
from urllib3.backend import ResponsePromise
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.response import HTTPResponse


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize(
    "status, body_kind",
    [
        (303, "file"),
        (303, "auto_file"),
        (303, "iterable"),
        (307, "file"),
        (307, "iterable"),
        (308, "file"),
        (308, "iterable"),
    ],
)
@pytest.mark.parametrize("chunked_via", ["kwarg", "header"])
async def test_redirect_body_state(
    monkeypatch: pytest.MonkeyPatch,
    asynchronous: bool,
    managed: bool,
    deferred: bool,
    status: int,
    body_kind: str,
    chunked_via: str,
) -> None:
    """Exercise immediate/deferred redirects through both pools and managers."""
    manager_cls: typing.Any = AsyncPoolManager if asynchronous else PoolManager
    pool_cls: typing.Any = (
        AsyncHTTPConnectionPool if asynchronous else HTTPConnectionPool
    )
    response_cls: typing.Any = AsyncHTTPResponse if asynchronous else HTTPResponse
    owner = manager_cls() if managed else pool_cls("example.test")
    headers = {"Content-Type": "application/octet-stream", "X-Keep": "yes"}
    if chunked_via == "header":
        headers["tRaNsFeR-EnCoDiNg"] = "chunked"
    original_headers = headers.copy()
    body = (b"pay", b"load") if body_kind == "iterable" else io.BytesIO(b"payload")
    requests: list[tuple[str, bytes, bool, HTTPHeaderDict]] = []

    try:
        if managed:
            pool = owner.connection_from_host("example.test")
            if asynchronous:
                pool = await pool
        else:
            pool = owner

        def make_request(
            conn: typing.Any, method: str, url: str, **kwargs: typing.Any
        ) -> typing.Any:
            outgoing_body = kwargs["body"]
            if outgoing_body is None:
                data = b""
            elif hasattr(outgoing_body, "read"):
                data = outgoing_body.read()
            else:
                data = b"".join(outgoing_body)
            requests.append(
                (method, data, kwargs["chunked"], HTTPHeaderDict(kwargs["headers"]))
            )
            response = response_cls(
                status=status if len(requests) == 1 else 200,
                headers={"location": "/next"} if len(requests) == 1 else {},
            )
            if not deferred:
                return response

            # Supply protocol responses while exercising real promise registration,
            # get_response(), and redirect dispatch above the transport boundary.
            promise = ResponsePromise(conn, len(requests), [])

            def getresponse(**_kwargs: typing.Any) -> typing.Any:
                return response

            async def async_getresponse(**_kwargs: typing.Any) -> typing.Any:
                return response

            monkeypatch.setattr(
                conn, "getresponse", async_getresponse if asynchronous else getresponse
            )
            return promise

        async def async_make_request(
            *args: typing.Any, **kwargs: typing.Any
        ) -> typing.Any:
            return make_request(*args, **kwargs)

        monkeypatch.setattr(
            pool, "_make_request", async_make_request if asynchronous else make_request
        )
        response = owner.urlopen(
            "POST",
            "http://example.test/start" if managed else "/start",
            body=body,
            body_pos=0 if body_kind == "file" else None,
            headers=headers,
            chunked=chunked_via == "kwarg",
            retries=Retry(total=1, redirect=1),
            multiplexed=deferred,
        )
        if asynchronous:
            response = await response
        if deferred:
            response = owner.get_response(promise=response)
            if asynchronous:
                response = await response
        assert response.status == 200
    finally:
        closing = owner.clear() if managed else owner.close()
        if asynchronous:
            await closing

    assert len(requests) == 2
    assert requests[0][0:2] == ("POST", b"payload")
    method, data, chunked, redirected_headers = requests[1]
    if status == 303:
        assert (method, data, chunked) == ("GET", b"", False)
        assert "Transfer-Encoding" not in redirected_headers
        assert "Content-Type" not in redirected_headers
    else:
        assert (method, data, chunked) == ("POST", b"payload", chunked_via == "kwarg")
        assert redirected_headers == HTTPHeaderDict(original_headers)
    assert redirected_headers["X-Keep"] == "yes"
    assert headers == original_headers
