Asynchronous Guide
==================

.. currentmodule:: urllib3

Preamble
--------

urllib3.future ships with native async interfaces starting with the version 2.6.
We took great care at ensuring the synchronous and asynchronous interfaces are mirroring each other.

So the great news is that you don't have to learn anything new!

.. code-block:: bash

  $ python -m pip install "urllib3.future>=2.6.900"

Exploring Async
---------------

First things first, import the urllib3 module:

.. code-block:: python

    import urllib3

You'll need a :class:`~urllib3.AsyncPoolManager` instance to make asynchronous requests.
This object handles all of the details of connection pooling and task safety
so that you don't have to:

.. code-block:: python

    async with urllib3.AsyncPoolManager() as pm:
        resp = await pm.request("GET", "https://httpbin.org/robots.txt")

        # Print the returned data.
        print(await resp.data)
        # b"User-agent: *\nDisallow: /deny\n"

here ``request()`` returns a :class:`~urllib3.AsyncHTTPResponse` object.

Corresponding Classes
---------------------

Find bellow the corresponding public interfaces for your day-to-day usages.

- ``urllib3.PoolManager()`` counterpart is ``urllib3.AsyncPoolManager()``
- ``urllib3.HTTPConnectionPool()`` counterpart is ``urllib3.AsyncHTTPConnectionPool()``
- ``urllib3.HTTPSConnectionPool()`` counterpart is ``urllib3.AsyncHTTPSConnectionPool()``
- ``urllib3.ProxyManager()`` counterpart is ``urllib3.AsyncProxyManager()``

The interfaces are strictly equivalent in terms of exposed API.

.. warning:: Callbacks are now required to be async/awaitable in asynchronous mode like ``on_post_connection``.

The following models are also mirrored:

- ``urllib3.ResolverDescription`` counterpart is ``urllib3.AsyncResolverDescription``
- ``urllib3.HTTPResponse`` counterpart is ``urllib3.AsyncHTTPResponse``

.. note:: ``urllib3.ResponsePromise`` is used in both asynchronous and synchronous contexts.

Corresponding Functions
-----------------------

- ``urllib3.proxy_from_url()`` counterpart is ``urllib3.async_proxy_from_url()``
- ``urllib3.connection_from_url()`` counterpart is ``urllib3.async_connection_from_url()``

You get the idea! Let's build the solutions of tomorrow, together!

Asynchronous SOCKS Proxies
--------------------------

Never it has been easier to transform your synchronous code into asynchronous. Our contrib module
``urllib3.contrib.socks`` ships with both sync and async interfaces at no additional cost in dependencies
or overhead.

- ``urllib3.contrib.socks.SOCKSProxyManager()`` counterpart is ``urllib3.contrib.socks.AsyncSOCKSProxyManager()``

So you have everything you need to get started!
Check other parts of the documentation to learn about parameters and APIs in general.

Example Before / After
----------------------

PoolManager
~~~~~~~~~~~

Given the following synchronous example:

.. code-block:: python

    with urllib3.PoolManager() as pm:
        resp = pm.request("GET", "https://httpbin.org/robots.txt")

        # Print the returned data.
        print(resp.data)
        # b"User-agent: *\nDisallow: /deny\n"

Will be transformed into:

.. code-block:: python

    async with urllib3.AsyncPoolManager() as pm:
        resp = await pm.request("GET", "https://httpbin.org/robots.txt")

        # Print the returned data.
        print(await resp.data)
        # b"User-agent: *\nDisallow: /deny\n"

ProxyManager
~~~~~~~~~~~~

Given the following synchronous example:

.. code-block:: python

    import urllib3

    with urllib3.ProxyManager("https://localhost:3128/") as pm:
        resp = proxy.request("GET", "https://google.com/")

Will be transformed into:

.. code-block:: python

    import urllib3

    async with urllib3.AsyncProxyManager("https://localhost:3128/") as pm:
        resp = await proxy.request("GET", "https://google.com/")


AsyncHTTPResponse
------------------

In our asynchronous context, the responses will be of type ``AsyncHTTPResponse``.

The following properties and methods are awaitable:

- Property ``data``
- Method ``read(...)``
- Method ``json()``
- Method ``stream(...)``
- Method ``close()``
- Method ``drain_conn()``
- Method ``readinto(...)``

In addition to that, ``AsyncHTTPResponse`` ships with an async iterator.

Sending async iterable
----------------------

In our asynchronous APIs, you can send async iterable using the ``body=...`` keyword argument.
It is most useful when trying to send files that are IO bound, thus blocking your event loop needlessly.
