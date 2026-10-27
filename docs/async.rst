Asynchronous Guide
==================

.. currentmodule:: urllib3

urllib3.future provides native async APIs alongside its synchronous APIs.
They share request options and response models, with differences in how you
read bodies, iterate streams, and manage cancellation.

For a complete reusable client with executable source and CI checks, see
:ref:`cookbook-service-client`.

.. code-block:: bash

    python -m pip install urllib3-future

Making requests
---------------

Create an :class:`~urllib3.AsyncPoolManager` inside the event loop that will use
it. Reuse it across requests, and await your requests before leaving its context:

.. code-block:: python
    :name: async-request-example

    import asyncio
    import urllib3


    async def main():
        async with urllib3.AsyncPoolManager() as pm:
            response = await pm.request("GET", "https://httpbin.org/robots.txt")
            print(response.status)
            print(await response.data)


    asyncio.run(main())

The default ``preload_content=True`` reads the body before ``request()`` returns.
For large or long-lived responses, use :ref:`async-streaming` instead.

Corresponding APIs
------------------

.. list-table::
    :header-rows: 1

    * - Synchronous
      - Asynchronous
    * - ``PoolManager``
      - ``AsyncPoolManager``
    * - ``HTTPConnectionPool`` / ``HTTPSConnectionPool``
      - ``AsyncHTTPConnectionPool`` / ``AsyncHTTPSConnectionPool``
    * - ``ProxyManager``
      - ``AsyncProxyManager``
    * - ``HTTPResponse``
      - ``AsyncHTTPResponse``
    * - ``ResolverDescription``
      - ``AsyncResolverDescription``
    * - ``proxy_from_url()`` / ``connection_from_url()``
      - ``async_proxy_from_url()`` / ``async_connection_from_url()``

These names are available from ``urllib3``. ``ResponsePromise`` is shared by
both APIs. An async manager coordinates tasks sharing its connections; use
separate managers for separate event loops.

Reading responses
-----------------

Await ``response.data``, ``response.read()``, ``response.read1()``,
``response.readinto()``, ``response.json()``, ``response.drain_conn()``, and
``response.close()``.

``response.stream()`` returns an **async iterator**. Consume it with
``async for``; do not await ``response.stream()`` itself.

Use ``await response.read()`` to read the entire remaining body. Unlike the
synchronous API, ``await response.read(-1)`` is incremental for backward
compatibility. See :ref:`response-read-semantics` for the complete comparison.
Zero-byte reads return ``b""`` without consuming data or performing socket I/O.

.. _async-streaming:

Streaming a response
--------------------

Close the response in ``finally`` so application errors or an early exit from
the loop also clean up the stream:

.. code-block:: python
    :name: async-streaming-example

    import asyncio
    import urllib3


    async def main():
        async with urllib3.AsyncPoolManager() as pm:
            response = await pm.request(
                "GET", "https://httpbin.org/bytes/1024", preload_content=False
            )
            try:
                total = 0
                async for chunk in response.stream(64 * 1024):
                    total += len(chunk)
                print(total)
            finally:
                await response.close()


    asyncio.run(main())

Reading to completion normally releases the connection automatically. If you
want to discard the remaining body while permitting reuse, await
``response.drain_conn()``. Calling ``release_conn()`` alone does not consume
or cancel unread data. See :ref:`response-lifecycle` for protocol-specific
cleanup and shutdown behavior.

Bounded concurrency
-------------------

Connection count and task count are different: one HTTP/2 or HTTP/3 connection
can carry several requests. Use an application semaphore when you need to
limit the number of requests in progress:

.. code-block:: python
    :name: async-concurrency-example

    import asyncio
    import urllib3


    async def main():
        limit = asyncio.Semaphore(8)
        urls = ["https://httpbin.org/get"] * 3
        async with urllib3.AsyncPoolManager(maxsize=4) as pm:
            async def fetch(url):
                async with limit:
                    response = await pm.request(
                        "GET", url,
                        timeout=urllib3.Timeout(connect=5, read=10),
                        pool_timeout=5,
                    )
                    return response.status

            results = await asyncio.gather(
                *(fetch(url) for url in urls), return_exceptions=True
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            print(results)


    asyncio.run(main())

For streamed responses, keep the semaphore acquired until you have consumed or
closed the response. For very large input collections, use a bounded worker
queue rather than creating a task for every URL at once.

This example waits for all requests to settle before propagating an individual
failure, so one failed request does not prematurely close the shared manager.

Deferred responses
------------------

``multiplexed=True`` lets you send requests and collect their responses later.
HTTP/1.1 returns an immediate response, so check the result type:

.. code-block:: python
    :name: async-deferred-example

    import asyncio
    import urllib3


    async def main():
        async with urllib3.AsyncPoolManager() as pm:
            pending = [
                await pm.urlopen("GET", url, multiplexed=True)
                for url in ("https://httpbin.org/get", "https://httpbin.org/ip")
            ]
            for result in pending:
                if isinstance(result, urllib3.ResponsePromise):
                    response = await pm.get_response(promise=result)
                else:
                    response = result
                if response is None:
                    raise RuntimeError("No response could be collected")
                print(response.status, await response.data)


    asyncio.run(main())

Sending the request and retrieving its response can each raise network or retry
errors. See :ref:`multiplexed-responses` for retrieval order, ``None`` results,
and shutdown behavior.

Proxies
-------

Use :class:`~urllib3.AsyncProxyManager` for an HTTP or HTTPS proxy:

.. code-block:: python
    :name: async-proxy-example

    import asyncio
    import urllib3


    async def main():
        async with urllib3.AsyncProxyManager("http://localhost:3128/") as pm:
            response = await pm.request("GET", "https://httpbin.org/get")
            print(response.status)


    asyncio.run(main())

For SOCKS proxies, install ``urllib3-future[socks]`` and use
``urllib3.contrib.socks.AsyncSOCKSProxyManager``. The dependency is currently
capped at ``python-socks`` 2.8.1; version 3 is incompatible. See :ref:`proxies` for proxy
configuration and TLS verification.

Async callbacks
---------------

Callbacks such as ``on_post_connection`` must be async functions:

.. code-block:: python
    :name: async-callback-example

    import asyncio
    import urllib3


    async def inspect_connection(info):
        print(info.http_version, info.destination_address)


    async def main():
        async with urllib3.AsyncPoolManager() as pm:
            await pm.request(
                "GET", "https://httpbin.org/get",
                on_post_connection=inspect_connection,
            )


    asyncio.run(main())

.. _async-upload:

Sending an async iterable
-------------------------

The ``body`` argument accepts an async iterable of byte chunks. This example
performs file reads in an executor so they do not block the event loop:

.. code-block:: python
    :name: async-upload-example

    import asyncio
    import urllib3


    async def file_chunks(path):
        loop = asyncio.get_running_loop()
        with open(path, "rb") as source:
            while True:
                chunk = await loop.run_in_executor(None, source.read, 64 * 1024)
                if not chunk:
                    break
                yield chunk


    async def main():
        async with urllib3.AsyncPoolManager() as pm:
            response = await pm.request(
                "PUT", "https://httpbin.org/put", body=file_chunks("upload.bin"),
                retries=False, redirect=False,
            )
            print(response.status, await response.data)


    asyncio.run(main())

Create ``upload.bin`` before running this example. A generator cannot normally
be rewound, so this example disables automatic retries and redirects. Use a
replayable body when a request might need to be sent again.

Sharing a resolver
------------------

An async resolver instance can be shared by managers on its event loop. The
application owns an explicitly supplied instance and closes it after all its
users finish:

.. code-block:: python
    :name: async-shared-resolver-example

    import asyncio
    import sys
    import urllib3
    from urllib3.contrib.resolver._async import AsyncResolverDescription


    async def main(url):
        resolver = AsyncResolverDescription.from_url("doh+cloudflare://").new()
        try:
            async with urllib3.AsyncPoolManager(resolver=resolver, timeout=10) as first:
                print((await first.request("GET", url)).status)
            async with urllib3.AsyncPoolManager(resolver=resolver, timeout=10) as second:
                print((await second.request("GET", url)).status)
        finally:
            await resolver.close()


    asyncio.run(main(sys.argv[1]))

Use a resolver URL or description when you want the manager to create and own
its instance. See :ref:`resolver-lifetime` for composition and recycling.

.. _async-websocket:

WebSocket messages
------------------

Install ``urllib3-future[ws]`` or ``urllib3-future[ws-fast]``. Use an appropriate
``ws://`` / ``wss://`` URL; :ref:`websockets` explains backend selection and the
extended-CONNECT variants. This example runs a reader task while sending a
message, then closes the response when the conversation is finished:

.. code-block:: python
    :name: async-websocket-example

    import asyncio
    import sys
    import urllib3


    async def exchange(url):
        async with urllib3.AsyncPoolManager(timeout=10) as pm:
            response = await pm.urlopen("GET", url)
            reader = None
            try:
                extension = response.extension
                if extension is None:
                    raise RuntimeError(f"WebSocket was not accepted: {response.status}")

                async def receive():
                    while True:
                        message = await extension.next_payload()
                        if message is None:
                            return
                        print(message)

                reader = asyncio.create_task(receive())
                await extension.send_payload("Hello World!")
                await asyncio.sleep(1)  # Application-selected conversation duration.
            finally:
                try:
                    await response.close()
                finally:
                    if reader is not None:
                        if not reader.done():
                            reader.cancel()
                        await asyncio.gather(reader, return_exceptions=True)
            if reader is not None and not reader.cancelled():
                reader.result()  # Propagate receive errors after cleanup.


    asyncio.run(exchange(sys.argv[1]))

The server may send unsolicited messages. Read timeouts raise
``ReadTimeoutError``; ``None`` is a clean end of the extension stream. A read
timeout does not itself require discarding a WebSocket, so an application can
choose to continue reading. Other I/O or protocol errors can close it.
The example closes deliberately after one second rather than depending on a
particular server's echo behavior. See :ref:`async-cancellation` for cancellation
limits and :doc:`reference/contrib/webextensions` for the methods.

.. _sse-resumption:

Server-Sent Events and resumption
---------------------------------

For a single connection, use ``sse://`` for HTTPS or ``psse://`` for plain HTTP,
await ``extension.next_payload()``, and close the response in ``finally``.
The parsed payload is a ``ServerSentEvent``; its ``data`` is text and its
``retry`` field is a delay in **milliseconds**, not seconds.

The library does not automatically reconnect. This example adds a bounded
application policy: reconnect twice after EOF, retain the last event ID, and
send it as ``Last-Event-ID``. It uses a regular HTTP(S) URL with an explicit
SSE extension so the same extension can retain its ID state across successive
connections:

.. code-block:: python
    :name: async-sse-resumption-example

    import asyncio
    import sys
    import urllib3
    from urllib3.contrib.webextensions._async.sse import (
        AsyncServerSideEventExtensionFromHTTP,
    )


    async def consume(url):
        extension = AsyncServerSideEventExtensionFromHTTP()
        last_event_id = ""
        reconnect_delay = 1.0
        async with urllib3.AsyncPoolManager(timeout=30) as pm:
            for attempt in range(3):
                headers = {"Last-Event-ID": last_event_id} if last_event_id else {}
                response = await pm.request(
                    "GET", url, headers=headers, extension=extension,
                    preload_content=False, retries=False, redirect=False,
                )
                try:
                    if response.status == 204:
                        return  # The server asks the client to stop reconnecting.
                    if response.status != 200 or response.extension is None:
                        raise RuntimeError(f"SSE was not accepted: {response.status}")
                    while True:
                        event = await extension.next_payload()
                        if event is None:
                            break
                        print(event.event, event.data)
                        last_event_id = event.id  # An empty ID clears the cursor.
                        if event.retry is not None:
                            reconnect_delay = min(30.0, max(0.0, event.retry / 1000))
                finally:
                    await response.close()
                if attempt < 2:
                    await asyncio.sleep(reconnect_delay)


    asyncio.run(consume(sys.argv[1]))

This example limits reconnection delays to 30 seconds and propagates transport,
read-timeout, and processing errors. Adapt those policies to the application;
automatic request retries are separate from SSE resumption. A server must
implement ``Last-Event-ID`` replay for resumption to work, and the application
must decide when an event has been processed successfully before persisting
its cursor.

Reusing the extension sequentially preserves its event-ID state when a later
connection contains events without an ``id:`` field. Do not share an extension
between concurrent responses. For a fresh process, restore the cursor from
application storage and follow the server's replay contract. Metadata-only
blocks, ID resets, and raw event text are described in
:ref:`server-sent-events` and :doc:`reference/contrib/webextensions`.

.. _async-cancellation:

Timeouts and cancellation
-------------------------

Connect, read, and pool-acquisition timeouts cover different waits. They are
not a single deadline for an entire download, all retries, or application
processing; see :ref:`timeout-boundaries`.

Task safety does not guarantee that cancellation affects only one multiplexed
stream. Cancelling during connection acquisition, request sending, or response
setup can take a cleanup path that closes the connection, affecting other
HTTP/2 or HTTP/3 requests using it. Closing an already-returned response follows
the stream cleanup rules in :ref:`response-lifecycle`.

Keep cleanup in ``finally`` blocks and propagate ``asyncio.CancelledError`` after
cleanup. Repeated cancellation can interrupt awaited cleanup. Shielding a send
does not by itself define how the request body, response, or stream will be
finished; it is not a general cancellation policy.

On WASI, native Preview 3 cancellation and timeout enforcement have additional
limitations. See :doc:`wasi` before relying on these deadlines there.
