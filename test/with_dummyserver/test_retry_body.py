from __future__ import annotations

import asyncio
import gzip
import ssl
import typing
from contextlib import asynccontextmanager

import pytest

from urllib3 import (
    AsyncPoolManager,
    HttpVersion,
    PoolManager,
    ResponsePromise,
    Retry,
    Timeout,
)
from dummyserver.server import DEFAULT_CA, DEFAULT_CERTS
from urllib3._async.connectionpool import AsyncHTTPConnectionPool
from urllib3._async.response import AsyncHTTPResponse
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.exceptions import (
    DecodeError,
    MaxRetryError,
    ProtocolError,
    ReadTimeoutError,
)
from urllib3.response import HTTPResponse


@asynccontextmanager
async def retry_server(
    scenario: str,
    *,
    http2: bool = False,
) -> typing.AsyncGenerator[tuple[int, bytes, asyncio.Event, list[int]], None]:
    payload = b'{"delay": 0.001}' if scenario != "empty" else b""
    body = gzip.compress(payload) if scenario in ("gzip", "raw-gzip") else payload
    if scenario == "invalid-gzip":
        body = b"not a gzip stream"
    requests: list[int] = []
    writers: list[asyncio.StreamWriter] = []
    tasks: set[asyncio.Task[typing.Any]] = set()
    body_started = asyncio.Event()

    async def handle_http2(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        from jh2.config import H2Configuration  # type: ignore[import-untyped]
        from jh2.connection import H2Connection  # type: ignore[import-untyped]
        from jh2.events import RequestReceived  # type: ignore[import-untyped]

        h2 = H2Connection(config=H2Configuration(client_side=False))
        h2.initiate_connection()
        writer.write(h2.data_to_send())
        while True:
            data = await reader.read(65536)
            if not data:
                return
            for event in h2.receive_data(data):
                if isinstance(event, RequestReceived):
                    requests.append(id(writer))
                    first = len(requests) == 1
                    data = body if first else b"ok"
                    headers = [
                        (b":status", b"503" if first else b"200"),
                        (b"content-length", str(len(data)).encode()),
                    ]
                    if first and scenario == "gzip":
                        headers.append((b"content-encoding", b"gzip"))
                    h2.send_headers(event.stream_id, headers)
                    h2.send_data(event.stream_id, data, end_stream=True)
            writer.write(h2.data_to_send())
            await writer.drain()

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks.add(task)
        writers.append(writer)
        try:
            if http2:
                await handle_http2(reader, writer)
                return
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

    context = None
    if http2:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(DEFAULT_CERTS["certfile"], DEFAULT_CERTS["keyfile"])
        context.set_alpn_protocols(["h2"])
    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
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
            with HTTPConnectionPool(
                "127.0.0.1", port, maxsize=1, timeout=Timeout(connect=5, read=0.2)
            ) as pool:
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
                "127.0.0.1", port, maxsize=1, timeout=Timeout(connect=5, read=0.2)
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
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("http2", [False, True])
@pytest.mark.parametrize("scenario", ["plain", "gzip", "empty"])
async def test_retry_exhaustion_drains_body_and_reuses_connection(
    asynchronous: bool, http2: bool, scenario: str
) -> None:
    observed: list[typing.Any] = []

    class ObserveRetry(Retry):
        def increment(self, *args: typing.Any, **kwargs: typing.Any) -> Retry:
            response = kwargs.get("response")
            if response is not None:
                assert response._body is None
                observed.append(response)
            return super().increment(*args, **kwargs)

    async with retry_server(scenario, http2=http2) as (port, _, _, requests):
        url = f"{'https' if http2 else 'http'}://localhost:{port}/"
        options: dict[str, typing.Any] = dict(
            # The fixture listens on IPv4; localhost's IPv6 port may belong to another test.
            resolver="in-memory://default?hosts=localhost:127.0.0.1",
            maxsize=1,
            block=True,
            timeout=5,
            ca_certs=DEFAULT_CA,
            disabled_svn={HttpVersion.h3},
            background_watch_delay=None,
        )
        retry = ObserveRetry(total=0, status_forcelist=[503], cache_response_body=False)
        request_options: dict[str, typing.Any] = dict(
            retries=retry, preload_content=False, multiplexed=http2
        )

        def run_sync() -> None:
            with PoolManager(**options) as manager:
                with pytest.raises(MaxRetryError):
                    result = manager.urlopen("GET", url, **request_options)
                    if http2:
                        assert isinstance(result, ResponsePromise)
                        manager.get_response(promise=result)
                assert len(observed) == 1 and observed[0].closed
                assert observed[0]._body is None
                response = manager.urlopen("GET", url, retries=False)
                assert response.data == b"ok"
                assert response.version == (20 if http2 else 11)

        if asynchronous:
            async with AsyncPoolManager(**options) as manager:
                with pytest.raises(MaxRetryError):
                    result = await manager.urlopen("GET", url, **request_options)
                    if http2:
                        assert isinstance(result, ResponsePromise)
                        await manager.get_response(promise=result)
                assert len(observed) == 1 and observed[0].closed
                assert observed[0]._body is None
                response = await manager.urlopen("GET", url, retries=False)
                assert await response.data == b"ok"
                assert response.version == (20 if http2 else 11)
        else:
            await asyncio.get_running_loop().run_in_executor(None, run_sync)
        assert len(requests) == 2
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
