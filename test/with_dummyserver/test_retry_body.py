from __future__ import annotations

import asyncio
import gzip
import typing
from contextlib import asynccontextmanager

import pytest

from urllib3 import Retry
from urllib3._async.connectionpool import AsyncHTTPConnectionPool
from urllib3._async.response import AsyncHTTPResponse
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.exceptions import DecodeError, ProtocolError, ReadTimeoutError
from urllib3.response import HTTPResponse


@asynccontextmanager
async def retry_server(
    scenario: str,
) -> typing.AsyncGenerator[tuple[int, bytes, asyncio.Event, list[int]], None]:
    payload = b'{"delay": 0.001}' if scenario != "empty" else b""
    body = gzip.compress(payload) if scenario in ("gzip", "raw-gzip") else payload
    if scenario == "invalid-gzip":
        body = b"not a gzip stream"
    requests: list[int] = []
    writers: list[asyncio.StreamWriter] = []
    tasks: set[asyncio.Task[typing.Any]] = set()
    body_started = asyncio.Event()

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks.add(task)
        writers.append(writer)
        try:
            while True:
                await reader.readuntil(b"\r\n\r\n")
                requests.append(id(writer))
                first = len(requests) == 1
                status = b"503 Service Unavailable" if first else b"200 OK"
                data = body if first else b"ok"
                headers = b"HTTP/1.1 " + status + b"\r\n"
                if first and scenario in ("gzip", "raw-gzip", "invalid-gzip"):
                    headers += b"Content-Encoding: gzip\r\n"
                if first and scenario in ("gzip", "raw-gzip"):
                    headers += b"Transfer-Encoding: chunked\r\n\r\n"
                    data = b"%x\r\n" % len(data) + data + b"\r\n0\r\n\r\n"
                else:
                    size = len(data) + (10 if first and scenario == "truncated" else 0)
                    headers += b"Content-Length: %d\r\n\r\n" % size
                writer.write(headers)
                await writer.drain()
                if first and scenario in ("stalled", "cancel"):
                    body_started.set()
                    await reader.read()
                    return
                writer.write(data)
                await writer.drain()
                if first and scenario == "truncated":
                    return
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield (
            server.sockets[0].getsockname()[1],
            body if scenario == "raw-gzip" else payload,
            body_started,
            requests,
        )
    finally:
        server.close()
        await server.wait_closed()
        for writer in writers:
            writer.close()
        if tasks:
            await asyncio.gather(*tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("cache", [False, True])
@pytest.mark.parametrize(
    "scenario",
    ["plain", "gzip", "raw-gzip", "empty", "invalid-gzip", "truncated", "stalled"],
)
async def test_live_retry_body(asynchronous: bool, cache: bool, scenario: str) -> None:
    observed = []
    errors = {
        "invalid-gzip": DecodeError,
        "truncated": ProtocolError,
        "stalled": ReadTimeoutError,
    }
    async with retry_server(scenario) as (port, payload, _, requests):

        class BodyRetry(Retry):
            def get_retry_after(self, response: HTTPResponse) -> float | None:
                assert response._pool is not None and response._pool.pool is not None
                assert not response._pool.pool.busy
                observed.append(response.data if cache else response._body)
                return None

            async def async_get_retry_after(
                self, response: AsyncHTTPResponse
            ) -> float | None:
                assert response._pool is not None and response._pool.pool is not None
                assert not response._pool.pool.busy
                observed.append(await response.data if cache else response._body)
                return None

        retry = BodyRetry(total=1, status_forcelist=[503], cache_response_body=cache)

        def run_sync() -> None:
            with HTTPConnectionPool("127.0.0.1", port, maxsize=1, timeout=0.2) as pool:
                if cache and scenario in errors:
                    with pytest.raises(errors[scenario]):
                        pool.urlopen("GET", "/", retries=retry, preload_content=False)
                    assert not pool.pool.busy  # type: ignore[union-attr]
                    response = pool.urlopen("GET", "/", retries=False)
                else:
                    response = pool.urlopen(
                        "GET",
                        "/",
                        retries=retry,
                        preload_content=False,
                        decode_content=scenario != "raw-gzip",
                    )
                assert response.data == b"ok"

        if asynchronous:
            async with AsyncHTTPConnectionPool(
                "127.0.0.1", port, maxsize=1, timeout=0.2
            ) as pool:
                if cache and scenario in errors:
                    with pytest.raises(errors[scenario]):
                        await pool.urlopen(
                            "GET", "/", retries=retry, preload_content=False
                        )
                    assert not pool.pool.busy  # type: ignore[union-attr]
                    response = await pool.urlopen("GET", "/", retries=False)
                else:
                    response = await pool.urlopen(
                        "GET",
                        "/",
                        retries=retry,
                        preload_content=False,
                        decode_content=scenario != "raw-gzip",
                    )
                assert await response.data == b"ok"
        else:
            await asyncio.get_running_loop().run_in_executor(None, run_sync)
        assert len(requests) == 2
        assert observed == (
            [] if cache and scenario in errors else [payload if cache else None]
        )
        if scenario in ("plain", "gzip", "raw-gzip", "empty"):
            assert requests[0] == requests[1]


@pytest.mark.asyncio
async def test_cancel_retry_body_read() -> None:
    async with retry_server("cancel") as (port, _, body_started, requests):
        async with AsyncHTTPConnectionPool("127.0.0.1", port, maxsize=1) as pool:
            task = asyncio.create_task(
                pool.urlopen(
                    "GET",
                    "/",
                    preload_content=False,
                    retries=Retry(
                        total=1, status_forcelist=[503], cache_response_body=True
                    ),
                )
            )
            await asyncio.wait_for(body_started.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert pool.pool is not None and not pool.pool._cursors
            response = await asyncio.wait_for(
                pool.urlopen("GET", "/", retries=False), 2
            )
            assert await response.data == b"ok"
            assert len(requests) == 2
