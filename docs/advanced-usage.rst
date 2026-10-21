Advanced Usage
==============

.. currentmodule:: urllib3


.. _pool-behavior:

Customizing Pool Behavior
-------------------------

A :class:`~poolmanager.PoolManager` creates connection pools as needed. Pool
identity includes the origin and connection policy, including TLS, proxy, and
protocol settings. Requests to the same hostname can therefore use different
pools. For example, ordinary HTTPS and an HTTP/1.1-only WebSocket extension
can select separate pools; the extension's restrictions do not change the
manager's settings for other requests.

.. list-table:: Pool sizing defaults
    :header-rows: 1

    * - Setting
      - Meaning
      - Default
    * - ``PoolManager(num_pools=...)``
      - Capacity of the manager's active pool cache
      - ``10``
    * - ``PoolManager(maxsize=...)``
      - Connection capacity of each pool the manager creates
      - The manager's ``num_pools`` value if omitted
    * - ``HTTPConnectionPool(maxsize=...)`` / ``HTTPSConnectionPool(maxsize=...)``
      - Connection capacity of a directly constructed pool
      - ``1``

Set both manager dimensions explicitly when tuning them independently:

.. code-block:: python

    import urllib3

    with urllib3.PoolManager(num_pools=50, maxsize=10, block=True) as pm:
        response = pm.request(
            "GET", "https://httpbin.org/get", pool_timeout=5
        )
        print(response.status)

The built-in pool scheduler can reuse an HTTP/2 or HTTP/3 connection while it
still has other streams in progress. ``maxsize`` counts connections, not
concurrent requests. The peer's stream limits and the availability of a
connection for the next operation also affect when a request can proceed.
Use an application semaphore or worker queue to bound request concurrency;
see :doc:`async` for an example.

At capacity, connection acquisition can wait. In the current built-in scheduler
this can also happen with ``block=False``: it does not provide the upstream
queue behavior of creating disposable overflow connections. Use
``pool_timeout`` on a request to bound this wait. It is separate from the
connect and read timeouts; its default is ``None`` (no acquisition deadline).

These settings are not a process-wide socket limit. Different policies can
create separate pools, Happy Eyeballs can attempt multiple addresses, and
responses retained across manager shutdown can keep old connections alive
while new pools are created.

.. _stream:
.. _streaming_and_io:

Streaming and I/O
-----------------

With ``preload_content=True`` (the default), a normal request reads its response
body into memory before returning. Connection ownership is released
without a manual ``release_conn()`` call.

Use ``preload_content=False`` to return after the response headers and consume
the body incrementally. Protocol processing may already have buffered some
body data; further network reads happen as you consume the response.

.. code-block:: python
    :name: sync-streaming-example

    import urllib3

    with urllib3.PoolManager() as pm:
        response = pm.request(
            "GET", "https://httpbin.org/bytes/1024", preload_content=False
        )
        try:
            total = 0
            for chunk in response.stream(64 * 1024):
                total += len(chunk)
            print(total)
        finally:
            response.close()

The ``finally`` block also handles an early ``break`` or an exception while
processing a chunk. Reading to completion normally releases the connection
automatically. For the async equivalent, see :ref:`async-streaming`.

.. _response-lifecycle:

Consume, discard, or close a response
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
    :header-rows: 1
    :widths: 25 75

    * - Operation
      - Effect
    * - Read or stream the entire body
      - Consume the response and release ownership in normal managed use.
        A healthy connection can be reused.
    * - ``response.drain_conn()``
      - Read and discard the remaining wire body without decompressing it.
        This may perform network I/O and allows reuse when the exchange finishes
        successfully.
    * - ``response.close()``
      - Abandon the response and discard buffered data. An unfinished HTTP/1.1
        response requires connection disposal. For HTTP/2 and HTTP/3, an
        unfinished stream is cancelled while other streams can continue on a
        healthy connection. A reset-write or transport failure can still require
        closing the connection.
    * - ``response.release_conn()``
      - Return held connection ownership. This compatibility operation does not
        drain or abort unread data and is not a substitute for the operations
        above.

If the wire exchange has already completed, closing unread local buffers need
not close the connection. Close each response you stop consuming, including
when using a manager context; do not rely on garbage collection for cleanup.
For async responses, await ``drain_conn()`` and ``close()``.

Manager and pool shutdown
~~~~~~~~~~~~~~~~~~~~~~~~~

.. versionchanged:: 2.25.903
    Responses and promises returned before shutdown retain the connection
    ownership needed to finish reading or collecting their existing exchange.

``PoolManager.clear()`` and leaving a manager context close its pools. Returned
responses remain readable, and returned ``ResponsePromise`` objects remain
retrievable. Their connections are disposed of after the remaining responses
are consumed or closed. The manager can create new pools for later requests.
A directly closed connection pool rejects new requests.

A request that has not yet returned a response or promise can fail if shutdown
wins the race. Retries and redirects can also require new requests, so normally
collect promises and finish or close responses before shutting down their owner.
These rules apply to both APIs. Task cancellation has additional consequences;
see :ref:`async-cancellation`.

.. _response-read-semantics:

Choosing a read method
~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
    :header-rows: 1
    :widths: 24 38 38

    * - Operation
      - Synchronous response
      - Asynchronous response
    * - ``read()`` / ``read(None)``
      - Read the remaining body
      - Await to read the remaining body
    * - ``read(n)`` with ``n > 0``
      - Read up to ``n`` bytes, waiting for data or EOF
      - Await up to ``n`` bytes, waiting for data or EOF
    * - ``read(-1)`` (or another negative size)
      - Read the remaining body, since 2.25.902
      - Await an incremental read, retained for backward compatibility
    * - ``read1()`` / ``read1(-1)``
      - Incremental read; may wait for input
      - Await an incremental read; may wait for input
    * - ``read(0)`` / ``read1(0)``
      - Return ``b""`` without advancing the body or decoder
      - Await to obtain ``b""`` without advancing the body or decoder
    * - ``stream(n)``
      - Iterate with ``for``
      - Iterate with ``async for``

``stream(-1)`` uses incremental reads in both APIs. It can wait for input;
``-1`` does not mean non-blocking socket polling. Neither positive chunk sizes
nor incremental reads preserve TCP packet, TLS record, or HTTP frame boundaries.
Content decompression can also change chunk sizes.

File-like readers
~~~~~~~~~~~~~~~~~

A synchronous response can be wrapped by file-like consumers. Keep the response
lifetime explicit even if the consumer only reads a prefix:

.. code-block:: python
    :name: buffered-response-example

    import io
    import urllib3

    with urllib3.PoolManager() as pm:
        response = pm.request(
            "GET", "https://httpbin.org/bytes/1024", preload_content=False
        )
        try:
            with io.BufferedReader(response, buffer_size=8192) as reader:
                print(reader.read(4))
        finally:
            response.close()

For complete JSON responses, ``response.json()`` handles decoding directly.
For incremental text processing, use an incremental decoder so a multibyte
character split across chunks is preserved.

.. _proxies:

Proxies
-------

You can use :class:`~poolmanager.ProxyManager` to tunnel requests through an
HTTP proxy:

.. code-block:: python

    import urllib3

    proxy = urllib3.ProxyManager("https://localhost:3128/")
    proxy.request("GET", "https://google.com/")

The usage of :class:`~poolmanager.ProxyManager` is the same as
:class:`~poolmanager.PoolManager`.

You can connect to a proxy using HTTP, HTTPS or SOCKS. urllib3's behavior will
be different depending on the type of proxy you selected and the destination
you're contacting.

Note that regardless of HTTP version support, the tunneling will always start a HTTP/1.1 connection.
HTTP/2 can be negotiated afterward. Also note that using a proxy disable HTTP/3 if supported, the connection
will never be upgraded.

HTTP and HTTPS Proxies
~~~~~~~~~~~~~~~~~~~~~~

Both HTTP/HTTPS proxies support HTTP and HTTPS destinations. The only
difference between them is if you need to establish a TLS connection to the
proxy first. You can specify which proxy you need to contact by specifying the
proper proxy scheme. (i.e ``http://`` or ``https://``)

urllib3's behavior will be different depending on your proxy and destination:

* HTTP proxy + HTTP destination
   Your request will be forwarded with the `absolute URI
   <https://tools.ietf.org/html/rfc7230#section-5.3.2>`_.

* HTTP proxy + HTTPS destination
    A TCP tunnel will be established with a `HTTP
    CONNECT <https://tools.ietf.org/html/rfc7231#section-4.3.6>`_. Afterward a
    TLS connection will be established with the destination and your request
    will be sent.

* HTTPS proxy + HTTP destination
    A TLS connection will be established to the proxy and later your request
    will be forwarded with the `absolute URI
    <https://tools.ietf.org/html/rfc7230#section-5.3.2>`_.

* HTTPS proxy + HTTPS destination
    A TLS-in-TLS tunnel will be established.  An initial TLS connection will be
    established to the proxy, then an `HTTP CONNECT
    <https://tools.ietf.org/html/rfc7231#section-4.3.6>`_ will be sent to
    establish a TCP connection to the destination and finally a second TLS
    connection will be established to the destination. You can customize the
    :class:`ssl.SSLContext` used for the proxy TLS connection through the
    ``proxy_ssl_context`` argument of the :class:`~poolmanager.ProxyManager`
    class.

For HTTPS proxies we also support forwarding your requests to HTTPS destinations with
an `absolute URI <https://tools.ietf.org/html/rfc7230#section-5.3.2>`_ if the
``use_forwarding_for_https`` argument is set to ``True``. We strongly recommend you
**only use this option with trusted or corporate proxies** as the proxy will have
full visibility of your requests.

Configure HTTPS proxy trust and client certificates in ``proxy_ssl_context``,
and proxy identity checks with ``proxy_assert_hostname`` or
``proxy_assert_fingerprint``. An explicit proxy context keeps its own certificate
policy and trust store; destination TLS settings do not override them. Destination
SNI, identity assertions, and client certificate parameters do not apply to
forwarding proxy handshakes. When no proxy context is supplied, the pool's TLS
policy remains the fallback, including ``ssl_context`` for forwarding connections.
This applies to both ``ProxyManager`` and ``AsyncProxyManager``.

.. _https_proxy_error_http_proxy:

Your proxy appears to only use HTTP and not HTTPS
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If you're receiving the :class:`~urllib3.exceptions.ProxyError` and it mentions
your proxy only speaks HTTP and not HTTPS here's what to do to solve your issue:

If you're using ``urllib3`` directly, make sure the URL you're passing into :class:`urllib3.ProxyManager`
starts with ``http://`` instead of ``https://``:

.. code-block:: python

     # Do this:
     http = urllib3.ProxyManager("http://...")
     
     # Not this:
     http = urllib3.ProxyManager("https://...")

If instead you're using ``urllib3`` through another library like Requests
there are multiple ways your proxy could be mis-configured. You need to figure out
where the configuration isn't correct and make the fix there. Some common places
to look are environment variables like ``HTTP_PROXY``, ``HTTPS_PROXY``, and ``ALL_PROXY``.

Ensure that the values for all of these environment variables starts with ``http://``
and not ``https://``:

.. code-block:: bash

     # Check your existing environment variables in bash
     $ env | grep "_PROXY"
     HTTP_PROXY=http://127.0.0.1:8888
     HTTPS_PROXY=https://127.0.0.1:8888  # <--- This setting is the problem!
     
     # Make the fix in your current session and test your script
     $ export HTTPS_PROXY="http://127.0.0.1:8888"
     $ python test-proxy.py  # This should now pass.
     
     # Persist your change in your shell 'profile' (~/.bashrc, ~/.profile, ~/.bash_profile, etc)
     # You may need to logout and log back in to ensure this works across all programs.
     $ vim ~/.bashrc

If you're on Windows or macOS your proxy may be getting set at a system level.
To check this first ensure that the above environment variables aren't set
then run the following:

.. code-block:: bash

    $ python -c 'import urllib.request; print(urllib.request.getproxies())'

If the output of the above command isn't empty and looks like this:

.. code-block:: python

    {
      "http": "http://127.0.0.1:8888",
      "https": "https://127.0.0.1:8888"  # <--- This setting is the problem!
    }

Search how to configure proxies on your operating system and change the ``https://...`` URL into ``http://``.
After you make the change the return value of ``urllib.request.getproxies()`` should be:

.. code-block:: python

    {  # Everything is good here! :)
      "http": "http://127.0.0.1:8888",
      "https": "http://127.0.0.1:8888"
    }

If you still can't figure out how to configure your proxy after all these steps
please `create an issue <https://github.com/jawah/urllib3.future>`_ and we'll try to help you with your issue.

SOCKS Proxies
~~~~~~~~~~~~~


For SOCKS, you can use :class:`~contrib.socks.SOCKSProxyManager` to connect to
SOCKS4 or SOCKS5 proxies. In order to use SOCKS proxies you will need to
install `python-socks <https://pypi.org/project/python-socks/>`_ or install urllib3-future with
the ``socks`` extra:

.. code-block:: bash

     python -m pip install urllib3.future[socks]

Once python-socks is installed, you can use
:class:`~contrib.socks.SOCKSProxyManager`:

.. code-block:: python

    from urllib3.contrib.socks import SOCKSProxyManager

    proxy = SOCKSProxyManager("socks5h://localhost:8889/")
    proxy.request("GET", "https://google.com/")

.. note::
      It is recommended to use ``socks5h://`` or ``socks4a://`` schemes in
      your ``proxy_url`` to ensure that DNS resolution is done from the remote
      server instead of client-side when connecting to a domain name.

.. _ssl_custom:
.. _custom_ssl_certificates:

Custom TLS Certificates
-----------------------

Instead of using `certifi <https://certifi.io/>`_ you can provide your
own certificate authority bundle. This is useful for cases where you've
generated your own certificates or when you're using a private certificate
authority. Just provide the full path to the certificate bundle when creating a
:class:`~poolmanager.PoolManager`:

.. code-block:: python

    import urllib3

    http = urllib3.PoolManager(
        cert_reqs="CERT_REQUIRED",
        ca_certs="/path/to/your/certificate_bundle"
    )
    resp = http.request("GET", "https://example.com")

When you specify your own certificate bundle only requests that can be
verified with that bundle will succeed. It's recommended to use a separate
:class:`~poolmanager.PoolManager` to make requests to URLs that do not need
the custom certificate.

.. _sni_custom:

Custom SNI Hostname
-------------------

If you want to create a connection to a host over HTTPS which uses SNI, there
are two places where the hostname is expected. It must be included in the Host
header sent, so that the server will know which host is being requested. The
hostname should also match the certificate served by the server, which is
checked by urllib3.

Normally, urllib3 takes care of setting and checking these values for you when
you connect to a host by name. However, it's sometimes useful to set a
connection's expected Host header and certificate hostname (subject),
especially when you are connecting without using name resolution. For example,
you could connect to a server by IP using HTTPS like so:

.. code-block:: python

    import urllib3

    pool = urllib3.HTTPSConnectionPool(
        "104.154.89.105",
        server_hostname="badssl.com"
    )
    pool.request(
        "GET",
        "/",
        headers={"Host": "badssl.com"},
        assert_same_host=False
    )


Note that when you use a connection in this way, you must specify
``assert_same_host=False``.

This is useful when DNS resolution for ``example.org`` does not match the
address that you would like to use. The IP may be for a private interface, or
you may want to use a specific host under round-robin DNS.


.. _assert_hostname:

Verifying TLS against a different host
--------------------------------------

If the server you're connecting to presents a different certificate than the
hostname or the SNI hostname, you can use ``assert_hostname``:

.. code-block:: python

    import urllib3

    pool = urllib3.HTTPSConnectionPool(
        "wrong.host.badssl.com",
        assert_hostname="badssl.com",
    )
    pool.request("GET", "/")


.. _ssl_client:

Client Certificates
-------------------

You can also specify a client certificate. This is useful when both the server
and the client need to verify each other's identity. Typically these
certificates are issued from the same authority. To use a client certificate,
provide the full path when creating a :class:`~poolmanager.PoolManager`:

.. code-block:: python

    http = urllib3.PoolManager(
        cert_file="/path/to/your/client_cert.pem",
        cert_reqs="CERT_REQUIRED",
        ca_certs="/path/to/your/certificate_bundle"
    )

If you have an encrypted client certificate private key you can use
the ``key_password`` parameter to specify a password to decrypt the key.

.. code-block:: python

    http = urllib3.PoolManager(
        cert_file="/path/to/your/client_cert.pem",
        cert_reqs="CERT_REQUIRED",
        key_file="/path/to/your/client.key",
        key_password="keyfile_password"
    )

If your key isn't encrypted the ``key_password`` parameter isn't required.

TLS minimum and maximum versions
--------------------------------

When the configured TLS versions by urllib3 aren't compatible with the TLS versions that
the server is willing to use you'll likely see an error like this one:

.. code-block::

    SSLError(1, '[SSL: UNSUPPORTED_PROTOCOL] unsupported protocol (_ssl.c:1124)')

Starting in v2.0 by default urllib3 uses TLS 1.2 and later so servers that only support TLS 1.1
or earlier will not work by default with urllib3.

To fix the issue you'll need to use the ``ssl_minimum_version`` option along with the `TLSVersion enum`_
in the standard library ``ssl`` module to configure urllib3 to accept a wider range of TLS versions.

For the best security it's a good idea to set this value to the version of TLS that's being used by the
server. For example if the server requires TLS 1.0 you'd configure urllib3 like so:

.. code-block:: python
    
    import ssl
    import urllib3
    
    http = urllib3.PoolManager(
        ssl_minimum_version=ssl.TLSVersion.TLSv1
    )
    # This request works!
    resp = http.request("GET", "https://tls-v1-0.badssl.com:1010")

.. _TLSVersion enum: https://docs.python.org/3/library/ssl.html#ssl.TLSVersion

.. _ssl_mac:
.. _certificate_validation_and_mac_os_x:

Certificate Validation and macOS
--------------------------------

Apple-provided Python and OpenSSL libraries contain a patches that make them
automatically check the system keychain's certificates. This can be
surprising if you specify custom certificates and see requests unexpectedly
succeed. For example, if you are specifying your own certificate for validation
and the server presents a different certificate you would expect the connection
to fail. However, if that server presents a certificate that is in the system
keychain then the connection will succeed.

`This article <https://hynek.me/articles/apple-openssl-verification-surprises/>`_
has more in-depth analysis and explanation.

.. _ssl_warnings:

TLS Warnings
------------

urllib3 will issue several different warnings based on the level of certificate
verification support. These warnings indicate particular situations and can
be resolved in different ways.

* :class:`~exceptions.InsecureRequestWarning`
    This happens when a request is made to an HTTPS URL without certificate
    verification enabled. Follow the :ref:`certificate verification <ssl>`
    guide to resolve this warning.

.. _disable_ssl_warnings:

Making unverified HTTPS requests is **strongly** discouraged, however, if you
understand the risks and wish to disable these warnings, you can use :func:`~urllib3.disable_warnings`:

.. code-block:: python

    import urllib3
    
    urllib3.disable_warnings()

Alternatively you can capture the warnings with the standard :mod:`logging` module:

.. code-block:: python

    logging.captureWarnings(True)

Finally, you can suppress the warnings at the interpreter level by setting the
``PYTHONWARNINGS`` environment variable or by using the
`-W flag <https://docs.python.org/3/using/cmdline.html#cmdoption-w>`_.

Brotli Encoding
---------------

Brotli is a compression algorithm created by Google with better compression
than gzip and deflate and is supported by urllib3 if the
`Brotli <https://pypi.org/Brotli>`_ package or
`brotlicffi <https://github.com/python-hyper/brotlicffi>`_ package is installed.
You may also request the package be installed via the ``urllib3[brotli]`` extra:

.. code-block:: bash

    $ python -m pip install urllib3.future[brotli]

Here's an example using brotli encoding via the ``Accept-Encoding`` header:

.. code-block:: python

    import urllib3

    urllib3.request(
        "GET",
        "https://www.google.com/",
        headers={"Accept-Encoding": "br"}
    )

Zstandard Encoding
------------------

`Zstandard <https://datatracker.ietf.org/doc/html/rfc8878>`_
is a compression algorithm created by Facebook with better compression
than brotli, gzip and deflate (see `benchmarks <https://facebook.github.io/zstd/#benchmarks>`_)
and is supported by urllib3 if the `zstandard package <https://pypi.org/project/zstandard/>`_ is installed.
You may also request the package be installed via the ``urllib3.future[zstd]`` extra:

.. code-block:: bash

    $ python -m pip install urllib3.future[zstd]

.. note::

    Zstandard support in urllib3 requires using v0.18.0 or later of the ``zstandard`` package.
    If the version installed is less than v0.18.0 then Zstandard support won't be enabled.

Here's an example using zstd encoding via the ``Accept-Encoding`` header:

.. code-block:: python

    import urllib3

    urllib3.request(
        "GET",
        "https://www.facebook.com/",
        headers={"Accept-Encoding": "zstd"}
    )


Decrypting Captured TLS Sessions with Wireshark
-----------------------------------------------
Python 3.8 and higher support logging of TLS pre-master secrets.
With these secrets tools like `Wireshark <https://wireshark.org>`_ can decrypt captured
network traffic.

To enable this simply define environment variable `SSLKEYLOGFILE`:

.. code-block:: bash

    export SSLKEYLOGFILE=/path/to/keylogfile.txt

Then configure the key logfile in `Wireshark <https://wireshark.org>`_, see
`Wireshark TLS Decryption <https://wiki.wireshark.org/TLS#TLS_Decryption>`_ for instructions.

Custom SSL Contexts
-------------------

You can exercise fine-grained control over the urllib3 SSL configuration by
providing a :class:`ssl.SSLContext <python:ssl.SSLContext>` object. For purposes
of compatibility, we recommend you obtain one from
:func:`~urllib3.util.create_urllib3_context`.

Once you have a context object, you can mutate it to achieve whatever effect
you'd like. For example, the code below loads the default SSL certificates, sets
the :data:`ssl.OP_ENABLE_MIDDLEBOX_COMPAT<python:ssl.OP_ENABLE_MIDDLEBOX_COMPAT>`
flag that isn't set by default, and then makes a HTTPS request:

.. code-block:: python

    import ssl

    from urllib3 import PoolManager
    from urllib3.util import create_urllib3_context

    ctx = create_urllib3_context(ssl_backend="ssl")
    ctx.load_default_certs()
    ctx.options |= ssl.OP_ENABLE_MIDDLEBOX_COMPAT

    with PoolManager(ssl_context=ctx, ssl_backend="ssl") as pool:
        pool.request("GET", "https://www.google.com/")

Note that this is different from passing an ``options`` argument to
:func:`~urllib3.util.create_urllib3_context` because we don't overwrite
the default options: we only add a new one.

.. _protocol-selection:

Remembering HTTP/3 over QUIC support
------------------------------------

For HTTPS over TCP, TLS ALPN negotiates an allowed HTTP version with the server.
An ``Alt-Svc`` response header can advertise an HTTP/3 endpoint for later use.
A custom DNS resolver that supplies HTTPS records can also discover HTTP/3
before the first TCP connection, when ``quic_upgrade_via_dns_rr`` is enabled.
HTTP/3 requires the optional ``qh3`` dependency and a supported platform.

The protocol used by a request is reported as ``response.version``: ``11``,
``20``, or ``30``. Availability depends on the server, installed dependencies,
resolver, proxy configuration, and allowed protocol versions. A proxy route can
prevent an otherwise available HTTP/3 upgrade.

Use ``preemptive_quic_cache`` to share discovered HTTP/3 endpoints between
managers, or to supply entries saved by your application:

.. code-block:: python

    from urllib3 import PoolManager

    quic_cache = {}
    with PoolManager(preemptive_quic_cache=quic_cache) as pm:
        response = pm.request("GET", "https://www.cloudflare.com/")
        print(response.version)

The cache accepts a mutable mapping from ``(host, port)`` to an alternative
``(host, port)`` or ``None``. Entries depend on discovery and successful use;
the example does not guarantee a particular entry or protocol. Coordinate
access if your application shares the mapping across threads, and manage
persistence yourself. Cached Alt-Svc entries cannot switch hostnames.

.. _explicitly-disable-http-2-and-or-http-3:

Selecting HTTP versions
-----------------------

Pass ``disabled_svn`` as a set of :class:`~urllib3.backend.HttpVersion` values to a
manager or connection pool:

.. code-block:: python

    from urllib3 import PoolManager, HttpVersion

    with PoolManager(disabled_svn={HttpVersion.h2, HttpVersion.h3}) as pm:
        response = pm.request("GET", "https://httpbin.org/get")
        print(response.version)  # 11

HTTP/1.1 can also be disabled. For a plain ``http://`` endpoint known to speak
HTTP/2, disabling HTTP/1.1 selects :ref:`HTTP/2 prior knowledge <h2-prior-knowledge>`.
Restrictions do not add capabilities to a server: a request can fail if no
allowed protocol is usable. Web extensions can impose additional restrictions,
which participate in :ref:`pool selection <pool-behavior>`.

.. _multiplexed-responses:

Multiplexed connections and deferred responses
----------------------------------------------

Ordinary ``request()`` calls can already use HTTP/2 or HTTP/3 and share their
connections with requests from other threads or tasks. ``multiplexed=True``
additionally separates issuing a request from retrieving its response.

On a multiplexed connection, the initial call returns a
:class:`~urllib3.backend.ResponsePromise`. Sending the request body still happens
as part of that call; a promise does not create a background upload task.
On HTTP/1.1, the same call returns an immediate response instead. Check the
return type so the code also works when multiplexing is unavailable:

.. code-block:: python
    :name: sync-deferred-example

    import urllib3

    with urllib3.PoolManager() as pm:
        pending = [
            pm.urlopen("GET", url, multiplexed=True)
            for url in ("https://httpbin.org/get", "https://httpbin.org/ip")
        ]
        for result in pending:
            if isinstance(result, urllib3.ResponsePromise):
                response = pm.get_response(promise=result)
            else:
                response = result
            if response is None:
                raise RuntimeError("No response could be collected")
            print(response.status, response.data)

``get_response(promise=...)`` requests that particular response.
``get_response()`` without a promise selects an available pending exchange;
there is no guaranteed global arrival order across pools. Retrieval can perform
network I/O and wait for response data. It is not a non-blocking socket poll.
``None`` means no response could be collected by that call, for example because
no pending exchange is available or another caller holds the required pool.
It does not prove that other tasks have finished their requests.

Errors may occur when issuing the request or later during ``get_response()``.
Read timeouts and retry handling apply during retrieval. Collect promises inside
the manager context in normal use; see :ref:`response-lifecycle` for the shutdown
guarantees introduced in 2.25.903. An HTTP/3 upgrade may need to wait until active
exchanges have finished. The :doc:`async` guide shows the equivalent awaited API.

Associate a promise with its response
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``response.is_from_promise(promise)`` identifies the originating request. Use it
when collecting without a specified promise instead of assuming that requests
complete in submission order. A response collected with
``get_response(promise=promise)`` belongs to that promise.

In-memory client (mTLS) certificate
-----------------------------------

.. note:: Available since version 2.2

Using newly added ``cert_data`` and ``key_data`` arguments in ``HTTPSConnection``, ``HTTPSPoolConnection`` and ``PoolManager``.
you will be capable of passing the certificate along with its key without getting nowhere near your filesystem.

This feature compensate for the complete removal of ``pyOpenSSL``.

You may give your certificate to urllib3.future this way::

    with HTTPSConnectionPool(
        self.host,
        self.port,
        key_data=CLIENT_INTERMEDIATE_KEY,
        cert_data=CLIENT_INTERMEDIATE_PEM,
    ) as https_pool:
        r = https_pool.request("GET", "/")

.. note:: If the selected backend cannot load an in-memory client identity, setup
    raises an error. Supply ``cert_file`` and ``key_file`` paths when in-memory
    certificate loading is unavailable.

Inspect connection information and timings
------------------------------------------

The library expose a keyword argument, namely ``on_post_connection=...`` that takes a single positional argument
of type ``ConnectionInfo``.

You can pass this named argument into any request methods in ``PoolManager`` or ``HTTP(S)ConnectionPool``.

.. note:: The class ``ConnectionInfo`` is exposed at top-level package import.

Here is a basic example on how to inspect a connection that was picked / created for your request::

    from urllib3 import PoolManager, ConnectionInfo

    def conn_callback(conn_info: ConnectionInfo) -> None:
        print(conn_info)

    with PoolManager(resolver="dot+google://") as pm:
        resp = pm.urlopen("GET", "https://pie.dev/get", on_post_connection=conn_callback)

``ConnectionInfo`` hold the following properties:

- established_latency *timedelta*
   - Time taken to establish the connection. Pure socket **connect**.
- http_version *HttpVersion*
   - HTTP protocol used with the remote peer (not the proxy).
- certificate_der *bytes*
- certificate_dict *dict*
   - The SSL certificate presented by the remote peer (not the proxy).
- issuer_certificate_der *bytes*
- issuer_certificate_dict *dict*
   - The SSL issuer certificate for the remote peer certificate (not the proxy).
- destination_address *tuple[str,int]*
   - The IP address used to reach the remote peer (not the proxy), that was yield by your resolver.
- cipher *str*
   - The TLS cipher used to secure the exchanges (not the proxy).
- tls_version *ssl.TLSVersion*
   - The TLS revision used (not the proxy).
- tls_handshake_latency *timedelta*
   - The time taken to reach a complete TLS liaison between the remote peer and us (not the proxy).
- resolution_latency *timedelta*
   - Time taken to resolve a domain name into a reachable IP address.
- request_sent_latency *timedelta*
   - Time taken to encode and send the whole request through the socket.

.. note:: Missing something valuable to you? Do not hesitate to ping us anytime. We will carefully study your request and implement it if we can.

Monitor upload progress
-----------------------

You can, since version 2.3.901, monitor upload progress.
To do so, pass on to the argument ``on_upload_body`` a callable that accept 4 positional arguments.

The arguments are as follow: ``total_sent: int, content_length: int | None, is_completed: bool, any_error: bool``.

- total_sent: Amount of bytes already sent
- content_length: Expected total bytes to be sent
- is_completed: Flag that indicate end of transmission (body)
- any_error: If anything goes wrong during upload, will be set to True

.. warning:: content_length might be set to ``None`` in case that we couldn't infer the actual body length. Can happen if body is an iterator or generator. In that case you still can manually provide a valid ``Content-Length`` header.

See the following example::

    from urllib3 import PoolManager

    def track(total_sent: int, content_length: int | None, is_completed: bool, any_error: bool) -> None:
        print(f"{total_sent} / {content_length} bytes", f"{is_completed=} {any_error=}")

    with PoolManager() as pm:
        resp = pm.urlopen("POST", "https://httpbin.org/post", body=b"foo"*1024*10, on_upload_body=track)

Using a Custom DNS Resolver
---------------------------

We take security matters very seriously. It is time that developers stop using insecure DNS resolution methods.

.. note:: Available since version 2.4, no additional dependencies are required. Everything is carefully made by urllib3.future developers.

urllib3.future allows you to avoid using the default, often insecure DNS, that ship with every other HTTP clients.
It strip you from having the deal with, often painful, extra steps to successfully integrate a custom resolver.

You can use any of the following DNS protocols:

- DNS over UDP (RFC 1035)
- DNS over TLS (RFC 7858)
- DNS over HTTPS (2 or 3) using ``application/dns-json`` or ``application/dns-message`` formats.
- DNS over QUIC

We explicitly choose not to support **DNSCrypt**. Support for this protocol must be brought by you
using our ``BaseResolver`` abstract class.

.. warning:: DNS over UDP is insecure and should only be used in a trusted networking environments. e.g. Your company isolated VLAN.

In addition to those, you can find three special "protocols":

- DNS using basic key-value dictionary (e.g. very much like a Hosts file)
- Disabled DNS resolution
- OS Resolver **(default)**

Upgrading to any of **DNS over TLS**, **DNS over HTTPS** or **DNS over QUIC** will dramatically increase your security
while consuming HTTP requests.

urllib3.future recommends the usage of **DNS over QUIC** or **DNS over HTTPS** to benefit from a substantial increase in
performance by leveraging a multiplexed connection.

.. note:: urllib3.future does not change the default resolver (OS by default). You'll have to specify it yourself.

You can add the optional keyword parameter ``resolver=...`` into your ``PoolManager`` and ``HTTP(S)PoolManager`` constructors.

``resolver=...`` takes either a ``Resolver``, a ``list[Resolver]`` or a ``str``.

The string is a URL representing how you want to configure your resolver. See bellow for how you can write said URLs
for each protocols.

.. note:: Thanks to our generic architecture, you can, at your own discretion combine multiple resolvers with or without specific conditions.

.. warning:: Using a hostname instead of an IP address is accepted when specifying your resolver. The caveat, here, is that the name resolution will proceed using your system default.

.. attention:: Only DNS over HTTPS support built-in support for proxies for now. We will support it in a future version.

DNS caching
~~~~~~~~~~~

The built-in network resolvers cache successful DNS resolutions in memory. This applies to DNS over UDP, TLS, HTTPS,
and QUIC. The system, null, and manual in-memory resolvers do not use this cache.

Each resolver instance owns an independent cache. Entries are retained for the shortest valid TTL found in the DNS
answer, capped by ``cache_max_ttl``. The default maximum TTL is 60 seconds. Answers with a zero, missing, malformed,
or otherwise unusable TTL are returned to the current caller but are not cached. Resolution failures and negative
answers are not cached, so a later call can retry immediately.

The cache is bounded and uses least-recently-used eviction. ``cache_maxsize`` controls the maximum number of resolution
entries retained by one resolver and defaults to 1024. Destination ports are not part of the cache identity: requests
for ``example.com:80`` and ``example.com:443`` can share one DNS resolution while each caller still receives its own
requested port.

Concurrent identical lookups are also coalesced. One thread or task performs the DNS exchange while the other callers
wait for the same success or failure. This single-flight coordination remains active when persistent caching is disabled.

Both options can be passed in a resolver URL::

    from urllib3 import PoolManager

    with PoolManager(
        resolver="doh+cloudflare://?cache_maxsize=256&cache_max_ttl=30"
    ) as pm:
        pm.urlopen(...)

They can also be passed as resolver constructor arguments::

    from urllib3.contrib.resolver.doh import HTTPSResolver

    resolver = HTTPSResolver(
        "cloudflare-dns.com",
        cache_maxsize=256,
        cache_max_ttl=30,
    )

Set ``cache_maxsize=0`` or ``cache_max_ttl=0`` to disable persistent caching for that resolver. These options discard
answers earlier than their authoritative TTL; they never extend an authoritative TTL.

DNS over UDP (Insecure)
~~~~~~~~~~~~~~~~~~~~~~~

Unanswered UDP questions are retried after approximately 333 milliseconds, with exponential backoff and jitter.
Questions that already received a valid response are not resent. Each lookup has a seven-second total budget by
default; a numeric resolver ``timeout`` replaces that budget, whether shorter or longer. For example,
``dou://1.1.1.1/?timeout=2`` allows two seconds for the entire exchange, including retries and waiting for another
reader. Unrelated or duplicate packets do not extend this deadline. Exhaustion raises ``socket.gaierror`` with
``EAI_AGAIN``. These retries apply only to UDP, not DNS over TCP or TLS.

If a matching UDP reply to an HTTPS question has the truncation flag set (``TC=1``) and reports no DNS error,
the HTTPS question is considered finished but its records are discarded. Resolution can still succeed using
the A/AAAA answers, without ECH configuration or HTTP/3 discovery from that HTTPS reply. The resulting lookup
is not cached, so later lookups can discover the missing HTTPS information. DNS over TCP and TLS are unaffected.

In order to specify your own DNS server over UDP you can specify it like so::

    from urllib3 import PoolManager

    with PoolManager(resolver="dou://1.1.1.1") as pm:
        pm.urlopen(...)

You can pass the following options to the DNS url:

- timeout
   - ``dou://1.1.1.1/?timeout=1.2``
- source_address
   - ``dou://1.1.1.1/?source_address=10.12.0.1:1111``
- cache_maxsize
   - Maximum number of cached resolution entries. Defaults to 1024.
- cache_max_ttl
   - Maximum cache lifetime in seconds. Defaults to 60.

.. warning:: DNS over UDP is generally to be avoided unless you are in a trusted networking environment.

DNS over TLS
~~~~~~~~~~~~

In order to specify your own DNS server over TLS you can specify it like so::

    from urllib3 import PoolManager

    with PoolManager(resolver="dot://1.1.1.1") as pm:
        pm.urlopen(...)

You can pass the following options to the DNS url:

- timeout
- source_address
- server_hostname
- key_file
- cert_file
- cert_reqs
- ca_certs
- ssl_version
- ciphers
- ca_cert_dir
- key_password
- ca_cert_data
- cert_data
- key_data
- cache_maxsize
- cache_max_ttl

DNS over QUIC
~~~~~~~~~~~~~

In order to specify your own DNS server over QUIC you can specify it like so::

    from urllib3 import PoolManager

    with PoolManager(resolver="doq://dns.nextdns.io") as pm:
        pm.urlopen(...)

You can pass the following options to the DNS url:

- timeout
- source_address
- server_hostname
- key_file
- cert_file
- cert_reqs
- ca_certs
- key_password
- ca_cert_data
- cert_data
- key_data
- cache_maxsize
- cache_max_ttl

DNS over HTTPS
~~~~~~~~~~~~~~

In order to specify your own DNS server over HTTPS you can specify it like so::

    from urllib3 import PoolManager

    with PoolManager(resolver="doh://dns.google") as pm:
        pm.urlopen(...)

You can pass the following options to the DNS url:

- timeout
- source_address
- headers
   - ``doh://dns.google/?headers=x-hello-world:goodbye&headers=x-client:awesome-urllib3-future``
   - Pass two header (**x-hello-world** with value **goodbye**, and **x-client** with value **awesome-urllib3-future**).
- server_hostname
- key_file
- cert_file
- cert_reqs
   - ``doh://dns.google/?cert_reqs=0`` -> Disable certificate verification (not recommended)
   - ``doh://dns.google/?cert_reqs=CERT_REQUIRED`` -> Enforce certificate verification (already the default)
- ca_certs
- ssl_version
   - ``doh://dns.google/?ssl_version=TLSv1.2`` -> Enforce TLS 1.2
- ciphers
- ca_cert_dir
- key_password
- ca_cert_data
- cert_data
- key_data
- disabled_svn
   - ``doh://dns.google/?disabled_svn=h3`` -> Disable HTTP/3
- proxy _(url)_
- proxy_headers
- keepalive_delay
- keepalive_idle_window
- cache_maxsize
- cache_max_ttl

.. warning:: DNS over HTTPS support HTTP/1.1, HTTP/2 and HTTP/3. By default it tries to negotiate HTTP/2, then if available negotiate HTTP/3. The server must provide a valid ``Alt-Svc`` in responses.

Some DNS servers over HTTPS may requires you to be properly authenticated. We allow, out of the box, three types of authentication:

- Basic Auth
- Bearer Token
- mTLS (or Client Certificate)

To forward a username and password::

    from urllib3 import PoolManager

    with PoolManager(resolver="doh://user:password@dns.google") as pm:
        pm.urlopen(...)

To pass a bearer token::

    from urllib3 import PoolManager

    with PoolManager(resolver="doh://token@dns.google") as pm:
        pm.urlopen(...)

Finally, to authenticate with a certificate::

    from urllib3 import PoolManager, ResolverDescription

    my_resolver = ResolverDescription.from_url("doh://dns.google")

    my_resolver["cert_data"] = ...  # also available: cert_file
    my_resolver["key_data"] = ...  # also available: key_file
    my_resolver["key_password"] = ... # optional keyfile decrypt password

    with PoolManager(resolver="doh://token@dns.google") as pm:
        pm.urlopen(...)

That's it! You can access almost every type of resolvers.

.. note:: The first two examples are exclusives to DNS over HTTPS while the third can be used with DNS over QUIC, and DNS over TLS.

You can leverage DNS over HTTPS using RFC 8484 or using the JSON format (JSON is not standard).
If you rather strictly follow standards with RFC 8484 with Google public DNS or Cloudflare public DNS, append the query parameter `?rfc8484=true` to your
DNS over HTTPS url.

Disable DNS
~~~~~~~~~~~

In order to forbid name resolution in general::

    from urllib3 import PoolManager

    with PoolManager(resolver="null://default") as pm:
        pm.urlopen(...)

This will block any attempt to reach a URL that isn't an IP address.

OS Resolver
~~~~~~~~~~~

To invoke your OS DNS default resolution mechanism::

    from urllib3 import PoolManager

    with PoolManager(resolver="system://default") as pm:
        pm.urlopen(...)

Doing this is strictly what happen by default.

Manual DNS Resolver
~~~~~~~~~~~~~~~~~~~

You can create your own tiny resolver that behave almost like the typical ``hosts`` file.

For example::

    from urllib3 import PoolManager

    with PoolManager(resolver="in-memory://default?hosts=example.com:1.1.1.1&hosts=example.dev:8.8.8.8") as pm:
        pm.urlopen(...)

.. note:: This is most useful in development or where you actually don't need a resolver in a highly controlled network environment.

Combining DNS Resolvers
~~~~~~~~~~~~~~~~~~~~~~~

You can leverage multiple DNS servers and resolution methods by passing an array of ``Resolver`` objects.
The given list will implicitly set order/preference.

For example::

    from urllib3 import PoolManager

    with PoolManager(resolver=["doh+google://default", "doh+cloudflare://default"]) as pm:
        pm.urlopen(...)

.. note:: This is meant for mission critical programs that require redundancy. There's no imposed limit on the resolver count. urllib3.future recommend not exceeding 5 resolvers.

Every proposed protocols can be mixed in.
Let's say, for some reasons, you wanted to forbid the resolution of `www.idontwantthis.tld`.
You would write the following::

    from urllib3 import PoolManager

    with PoolManager(resolver=["doh+google://default", "null://default?hosts=www.idontwantthis.tld"]) as pm:
        pm.urlopen(...)

This tiny code will prevent any resolution of `www.idontwantthis.tld`, therefore raising an exception if ever happening.

Multi-threading considerations
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

In normal conditions, this::

    from urllib3 import PoolManager

    with PoolManager(resolver=["doh+google://"]) as pm:
        pm.urlopen(...)

Open a single connection to Google DNS over HTTPS server. It will be a multiplexed connection by default.
So each time urllib3.future need to transform a hostname into a IP address, it will send up to 3 concurrent requests
(or questions) at once.

On a single threaded application, it will be more than enough to enjoy a fast experience.
The story isn't the same when you decide to spawn multiple threads. As per our thread safety policy, we lock a resolver
two times, once when we send the queries, and finally when we wait for the answers.

You will most likely reach a bottleneck when querying a lot of different domain names.
Fortunately, you can easily circumvent that limitation!

Simply put. Pass multiple DNS urls, duplicated or not::

    from urllib3 import PoolManager

    with PoolManager(resolver=["doh+google://", "doh+cloudflare://", "doh+cloudflare://", "doq+adguard://"]) as pm:
        pm.urlopen(...)

This example will provide you with 4 distinct resolver each having its own connection. They will be able to work
concurrently.

.. note:: It is scalable at will. So long that you own the required resources.

.. warning:: In a non multi-threaded environment, the first resolver is most likely to be used alone. No load-balancing is to be expected.

Restrict a resolver with specific domains
-----------------------------------------

Let's imagine you have a resolver that is only capable of translating domain like ``*.company.internal``.
How do you pass on a DNS url that is restricted with this?

Here's how::

    from urllib3 import PoolManager

    with PoolManager(resolver=["doh+google://", "dou://10.10.22.22/?hosts=*.company.internal"]) as pm:
        pm.urlopen(...)

With this, all domains that match ``*.company.internal`` will be resolved using ``10.10.22.22`` server.
Otherwise, DNS over HTTPS by Google will be tried.

.. note:: More complex infrastructure may have two or more top level domains. Use the comma in the **hosts** query parameter to separate entries. Like so: ``?hosts=*.company.internal,*.company.second-site`` or even ``?hosts=*.company.internal&hosts=*.company.second-site``.

Isn't nice?

.. warning:: To ensure that ``localhost`` can be resolved, urllib3.future always add a ``system://default/?hosts=localhost`` to the resolvers (if necessary).

Shortcut DNS for trusted providers
----------------------------------

When using the ``resolver=...``  using an URL, you can use some ready-to-use URLs.

We provide shortcuts for the following providers:

- Cloudflare
   - DNS over TLS: `dot+cloudflare://`
   - DNS over UDP: `dou+cloudflare://`
   - DNS over HTTPS: `doh+cloudflare://`
- Google
   - DNS over TLS: `dot+google://`
   - DNS over UDP: `dou+google://`
   - DNS over HTTPS: `doh+google://`
- AdGuard
   - DNS over TLS: `dot+adguard://`
   - DNS over UDP: `dou+adguard://`
   - DNS over HTTPS: `doh+adguard://`
   - DNS over QUIC: `doq+adguard://`
- Quad9
   - DNS over TLS: `dot+quad9://`
   - DNS over UDP: `dou+quad9://`
   - DNS over HTTPS: `doh+quad9://`
- OpenDNS _(Nothing to do with Open Source, belong to Cisco)_
   - DNS over TLS: `dot+opendns://`
   - DNS over UDP: `dou+opendns://`
   - DNS over HTTPS: `doh+opendns://`
- NextDNS
   - DNS over QUIC: `doq+nextdns://`
   - DNS over HTTPS: `doh+nextdns://`

.. note:: We very much welcome suggestions if you feel this list is incomplete. Probably is the case! We won't accept servers that are from your ISP.

.. warning:: Beware that, as of january 2024, both Google and Cloudflare does not support DNS over QUIC. To leverage a QUIC connection with them, you will have to use DNS over HTTPS.

How to choose your resolver? Simply check the latency of each. Thanks to urllib3.future, you can inspect ``ConnectionInfo``
using ``on_post_connection`` callback. Depending on various factors, you may find one more reactive than the other.

Using a custom port with a shortcut DNS url
-------------------------------------------

Some countries may be issuing restriction with specific ports, preventing you to simply put ``doh+cloudflare://`` for example.
You can easily circumvent this limitation by choosing another port, the provider can at his own discretion provide
alternative port or not.

- ``doh+cloudflare://default:8443``

Given example replace default port 443 with port 8443.

.. warning:: Cloudflare does not propose 8443 as an alternative port (it's just for the example). See https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/ for more.

Passing options to the Resolver
-------------------------------

You can pass almost all supported keyword arguments that come with ``urllib3.future`` like but not limited to:

- timeout
- source_address
- ssl_version
- cert_reqs
- assert_fingerprint
- assert_hostname
- ssl_minimum_version
- ssl_maximum_version
- cache_maxsize
- cache_max_ttl

.. note:: Beware that we automatically forward ``ca_cert_data``, `ca_cert_dir`, and ``ca_certs`` (if specified) for convenience if not specified in DNS parameters.

When passing the resolver as a plain string url, you can do as follow::

    from urllib3 import PoolManager

    with PoolManager(resolver="doh+google://default?timeout=2") as pm:
        pm.request("GET", "https://httpbin.org/get")

This sets a two-second resolver timeout and keeps certificate verification enabled.

It is also possible to pass more complex argument like **ca_cert_data** using a ``ResolverDescription`` instance::

    from urllib3 import PoolManager, ResolverDescription
    import wassima

    my_resolver = ResolverDescription.from_url("doh+google://default?timeout=2")
    my_resolver["ca_cert_data"] = wassima.generate_ca_bundle()

    with PoolManager(resolver=my_resolver) as pm:
        pm.request("GET", "https://httpbin.org/get")

The optional ``wassima`` package (``python -m pip install wassima``) reads the OS
trust store. The configured description is passed to the manager, so its CA data
is used to verify the DNS server.

Create your own Resolver
------------------------

You can create a resolver from scratch by inheriting ``BaseResolver`` that is located at ``urllib3.contrib.resolver``.
Then, once ready, you can instantiate it and pass it directly into the keyword argument ``resolver=...``.

The minimum viable resolver requires you to implement the methods ``getaddrinfo(...)``, ``close()``, and ``is_available()``.

Use cases:

- DNS over PostgreSQL (Using a database to translate hostnames)
- DNS over Redis (Implementing a sharable persistent cache)

.. note:: You can inherit any of ``urllib3.contrib.resolver.dot.TLSResolver``, ``urllib3.contrib.resolver.doq.QUICResolver``, ``urllib3.contrib.resolver.dou.PlainResolver``, or ``urllib3.contrib.resolver.doh.HTTPSResolver`` and add your own layer. e.g. the redis sharable cache layer.

DNSSEC
------

DNSSEC validation depends on the recursive resolver you configure.
urllib3.future does not validate DNSSEC signature chains locally. Choosing
DNS over TLS, HTTPS, or QUIC encrypts the DNS transport and, with certificate
verification enabled, authenticates the DNS server. It does not by itself
establish that the server validates DNSSEC.

A validating recursive resolver can reject a domain with bogus DNSSEC records,
which then surfaces as a resolution error in urllib3.future. For background,
see `Cloudflare's DNSSEC explanation
<https://www.cloudflare.com/learning/dns/dns-security/>`_.

The current resolver error messages classify DNS ``SERVFAIL`` (response code 2)
as a DNSSEC validation failure. ``SERVFAIL`` also has other causes; the message
alone is not proof of a failed signature check. Confirm the cause with your
resolver's diagnostics. The system resolver's validation policy likewise
belongs to the system or upstream DNS service.

Use our Resolvers outside of urllib3-future
-------------------------------------------

It is possible to do hostname resolution without having to issue a request, in the case if you are only
interested in that part.

This simple code demonstrate it::

    from urllib3 import ResolverDescription
    import socket

    resolver = ResolverDescription.from_url("doh+google://").new()
    res = resolver.getaddrinfo("www.cloudflare.com", 443, socket.AF_UNSPEC, socket.SOCK_STREAM)

.. note:: The method `getaddrinfo` behave exactly like the Python native implementation in the socket stdlib.

A keyword-parameter, namely `quic_upgrade_via_dns_rr`, should be set to **False** (already the default) to avoid
looking for the HTTPS record, thus taking you out of guard with the return list. We almost always start by looking
for a TCP entrypoint, but thanks to HTTPS records, we can return a UDP entrypoint in the results.

.. note:: Our resolvers are thread safe.

Refer to the API references to know more about exposed methods.

.. note:: You can use at your own discretion your instantiated resolver in a ``PoolManager``, ``HTTP(S)ConnectionPool`` or even ``HTTP(S)Connection`` using the ``resolver=...`` keyword argument.

Combine resolvers
~~~~~~~~~~~~~~~~~

Using multiple DNS resolvers is nearly as easy as instantiating a single one.
You may follow bellow example::

    from urllib3 import ResolverDescription
    from urllib3.contrib.resolver import ManyResolver

    resolvers = [
        ResolverDescription.from_url("doh+google://").new(),
        ResolverDescription.from_url("doh+cloudflare://").new(),
        ResolverDescription.from_url("doh+adguard://").new(),
        ResolverDescription.from_url("dot+google://").new(),
        ResolverDescription.from_url("doh+google://").new(),
    ]

    resolver = ManyResolver(*resolvers)
    # ....You know the drill..!

Enforce IPv4 or IPv6
--------------------

.. note:: Available since version 2.4+

You can enforce **urllib3.future** to connect to IPv4 addresses or IPv6 only.
To do so, you just have to specify the following keyword argument (``socket_family``) into
your ``PoolManager``, ``HTTP(S)ConnectionPool`` or ``HTTP(S)Connection``.

By writing exactly this::

    from urllib3 import PoolManager
    import socket

    with PoolManager(socket_family=socket.AF_INET) as pm:
        pm.urlopen("GET", "https://pie.dev/get", on_post_connection=lambda ci: print(ci))

In this example, you are enforcing connecting to a IPv4 only address, and thanks to the callback ``on_post_connection``
you will be able to inspect the ``ConnectionInfo`` and verify the destination address.

Happy Eyeballs
--------------

.. note:: Available since version 2.7+

Introduction
~~~~~~~~~~~~

Happy Eyeballs (also called Fast Fallback) is an algorithm published by the IETF that
makes dual-stack applications (those that understand both IPv4 and IPv6) more responsive
to users by attempting to connect using both IPv4 and IPv6 at the same time (preferring IPv6), thus
minimizing common problems experienced by users with imperfect IPv6 connections or setups.

The name "happy eyeballs" derives from the term "eyeball" to describe endpoints which represent
human Internet end-users, as opposed to servers.

Source: https://en.wikipedia.org/wiki/Happy_Eyeballs

Support
~~~~~~~

urllib3.future is capable of serving the Happy Eyeballs algorithm even if there is only one type
of addresses available (e.g. IPv4 OR IPv6).

We choose to limit the numbers of concurrent connections to 4 (by default) at the time we wrote this.

By default, for backward compatibility sake, it is disabled.
You will have to enable it this way::

    import urllib3

    with urllib3.PoolManager(happy_eyeballs=True) as pm:
        response = pm.request("GET", "https://httpbin.org/get")
        print(response.status)

.. note:: The keyword argument ``happy_eyeballs`` can be used in ``AsyncPoolManager``, ``AsyncHTTPConnectionPool`` and its synchronous counterparts.

The synchronous implementation falls back to sequential connection attempts
when threads are unavailable. See :doc:`wasi` for native socket limitations.

Debug
~~~~~

urllib3.future logs its attempt and help you track why it choose one method or the other.

Concurrency model
~~~~~~~~~~~~~~~~~

urllib3.future serve both synchronous and asynchronous interface, so we had to use two different concurrency models.
Happy Eyeballs requires to do concurrent tasks.

- Synchronous mode: ThreadPoolExecutor
- Asynchronous mode: Asyncio native tasks

.. note:: The asynchronous algorithm is more efficient than its synchronous counterpart.

.. warning:: A particularity exist in the synchronous context, a timeout is mandatory due its nature (threads: cannot kill them if pending I/O). By default we expect a max delay of 5000ms at most if no connect timeout is specified.

In what scenario do I gain using this?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Here are a few use cases where you may gain a substantial performance gain:

- I want to download a video from BigProvider XYZ (e.g. YouTube) that yield 10 IP addresses per DNS query. It will pick the fastest server available.
- I am inside of a Kubernetes environment, and I want to pick the fastest endpoint when the service has more than 1 IP tied to it.
- IPv6 fail sometime or IPv4, and I cannot predict it easily so I have to try them.

.. note:: In the last item "IPv6 fail sometime or IPv4", you may are facing the unusual "my connect timeout" isn't respected. It was due to attempting connect to several addresses sequentially, thus making the connect timeout applied "or waited" several times.

Change the number of concurrent connections
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

urllib3.future always try up to 4 IP addresses by default, but that behavior is easily overridable.
So, instead of passing a boolean, pass an integer that must be > 1, otherwise, it will be considered
as disabled.

Here is a simple example::

    import urllib3

    with urllib3.PoolManager(happy_eyeballs=10) as pm:
        response = pm.request("GET", "https://httpbin.org/get")
        print(response.status)

This permits up to 10 concurrent connection attempts. Fewer are started if the
resolver returns fewer candidate addresses. Async managers use tasks; synchronous
managers use threads.

Larger values increase temporary socket and scheduling costs. The default of
four attempts is a useful starting point.

.. _h2-prior-knowledge:

HTTP/2 with prior knowledge
---------------------------

.. note:: Available since version 2.8+

In some cases, mostly internal networks, you may desire to leverage multiplexing within a single HTTP connection without
bothering with TLS (ALPN extensions) to discover and use HTTP/2 capabilities.

You're in luck! urllib3-future now support talking with HTTP/2 server over an unencrypted connection.
The only things you have to do is to disable HTTP/1.1 so that we can infer that you want to negotiate HTTP/2
without any prior clear indicative that the remote can.

Here is a simple example::

    import urllib3

    with urllib3.PoolManager(disabled_svn={urllib3.HttpVersion.h11}) as pm:
        r = pm.urlopen("GET", "http://my-internal.svc.local/")


HTTP Trailers
-------------

.. note:: Available since version 2.9+

HTTP response may contain one or several trailer headers. Those special headers are received
after the reception of the body. Before this, those headers were unreachable and dropped silently.

Quoted from Mozilla MDN: "The Trailer response header allows the sender to include additional fields
at the end of chunked messages in order to supply metadata that might be dynamically generated while the
message body is sent, such as a message integrity check, digital signature, or post-processing status."

Here is a simple example::

    import urllib3

    with urllib3.PoolManager() as pm:
        r = pm.urlopen("GET", "https://example.test")
        print(r.trailers)

.. note:: The property ``trailers`` return either ``None`` or a fully constructed ``HTTPHeaderDict``.

.. warning:: ``None`` means we did not receive trailer headers (yet). If ``preload_content`` is set to False, you will need to consume the entire body before reaching the ``trailers`` property.

Informational / Early Responses
-------------------------------

.. note:: Available since version 2.10+

Sometimes, thanks to HTTP standards, a webserver may send intermediary responses before the main one for
a particular request.

All others HTTP client swallow them silently and you cannot see them. Thanks to urllib3-future, that
issue is a thing of the past.

You may now inspect early responses like https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/100 (Continue) or
https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/103 (Early Hint) with a mere callback.

A server may send zero or more informational responses. This example records
them without assuming that a 103 response will arrive:

.. code-block:: python
    :name: early-response-example

    from urllib3 import PoolManager

    early_responses = []

    def on_early_response(response):
        early_responses.append(response)

    with PoolManager() as pm:
        response = pm.request(
            "GET", "https://early-hints.fastlylabs.com/",
            on_early_response=on_early_response,
        )
        for early in early_responses:
            print(early.status, early.headers)
        print(response.status)

For asynchronous requests, use an ``async def`` callback and await the request;
see :doc:`async`. When using deferred responses, the callback can run during
response retrieval.

.. warning:: Some webservers may enable that feature on HTTP/2+ and disable it in HTTP/1.1. In urllib3-future case, that feature is available no matter the used protocol.

.. note:: Status 101 (Switching Protocols) is the final upgrade response and is
    not delivered through ``on_early_response``.

.. note:: Any responses yielded through the "on_early_response" callback will never have a body per standards.

Switching Protocol
------------------

A successful protocol switch exposes ``response.extension`` for exchanging
subprotocol data. For WebSockets, use the implementations in :doc:`user-guide`.
For a custom HTTP/1.1 upgrade, the following example requires a server on
``localhost:8080`` that accepts the illustrative ``my-protocol`` upgrade:

.. code-block:: python

    import urllib3

    with urllib3.PoolManager(
        disabled_svn={urllib3.HttpVersion.h2, urllib3.HttpVersion.h3}
    ) as pm:
        response = pm.urlopen(
            "GET", "http://localhost:8080/", timeout=10,
            headers={"Connection": "Upgrade", "Upgrade": "my-protocol"},
        )
        try:
            extension = response.extension
            if extension is None:
                raise RuntimeError(f"Upgrade was not accepted: {response.status}")
            extension.send_payload(b"hello")
            print(extension.next_payload())
        finally:
            response.close()

HTTP/2 and HTTP/3 use extended CONNECT rather than these HTTP/1.1 upgrade
headers. The server must advertise the required support. Select an extension
that supports that protocol, as described for WebSockets in the user guide.

Background unsolicited data
----------------------------

.. note:: Upgrade urllib3-future to 2.11+ or later to benefit from this.

Since HTTP/2 or later, you may receive unsolicited incoming data that can be a challenge to verify whether the
connection is still up or not. We added a discrete task that carefully check for incoming data in idle connections.

To customize the behavior you may add the parameter ``background_watch_delay`` to your PoolManager or ConnectionPool
instance constructor.

Setting it to ``None`` makes it disabled.

.. note:: By default, it checks for incoming data and react to it every 5000ms.

.. warning:: Disabling this will void the effect of our automated keepalive.

Server-Sent Events
------------------

SSE is supported over HTTP/1.1, HTTP/2, and HTTP/3. Use ``sse://`` for HTTPS or
``psse://`` for plain HTTP. Check for a successful extension before reading,
and for ``None`` at the end of the stream:

.. code-block:: python
    :name: sse-example

    import urllib3

    with urllib3.PoolManager() as pm:
        response = pm.urlopen(
            "GET", "sse://httpbingo.org/sse?delay=1s&duration=5s&count=10"
        )
        try:
            extension = response.extension
            if extension is None:
                raise RuntimeError(f"SSE request was not accepted: {response.status}")
            while True:
                event = extension.next_payload()
                if event is None:
                    break
                print(event.data)
            print(response.trailers)
        finally:
            response.close()

``next_payload()`` returns a parsed ``ServerSentEvent``. Its ``data`` is text;
call ``event.json()`` only when the server sends JSON. Passing ``raw=True``
returns the event text instead. The reader does not automatically reconnect
at EOF. Applications that reconnect can use event IDs and retry metadata to
implement the server's resumption policy.

Debug your pool state
---------------------

Ever wondered what is inside your ``PoolManager`` or ``AsyncPoolManager``?
Wonder no more! starting from urllib3-future 2.12.913+ you can have a proper representation of those objects.

.. code-block:: python

    import urllib3

    if __name__ == "__main__":

        with urllib3.PoolManager() as pm:
            r = pm.urlopen("GET", "https://httpbingo.org/get")
            print(pm)  # <PoolManager <HTTPSConnectionPool "httpbingo.org:443" <TrafficPolice 1/10 (Idle)>> <TrafficPolice 1/10 (Idle)>>

.. note:: TrafficPolice is the inner task/thread safety container. The representation is as follow: used/maxsize (status).

.. note:: TrafficPolice is either 'idle', 'used' or 'saturated'.

.. warning:: Both ``PoolManager`` and ``ConnectionPool`` have a ``TrafficPolice`` inside them.

.. danger:: Calling ``repr`` here is highly discouraged at a high frequency. Do it for debug purposes only.

.. _tls-backends:

Alternative TLS backends (``anytls``)
-------------------------------------

urllib3-future can transparently use one of three TLS backends:

* ``rtls`` - Rustls with AWS-LC (`PyPI <https://pypi.org/project/rtls/>`_)
* ``utls`` - BoringSSL (`PyPI <https://pypi.org/project/utls/>`_)
* ``ssl``  - the Python standard library (commonly OpenSSL or LibreSSL)

At import time, urllib3-future picks the best available backend, following the
default priority order:

::

    rtls  ->  utls  ->  ssl

You don't have to do anything to enable this. Just install the optional
extra you want and urllib3-future will pick it up automatically:

.. code-block:: bash

    # Use Rustls + AWS-LC
    pip install urllib3-future[rtls]

    # Use BoringSSL
    pip install urllib3-future[utls]

Forcing a specific backend
~~~~~~~~~~~~~~~~~~~~~~~~~~

Set the ``URLLIB3_FUTURE_SSL_BACKEND`` environment variable to force a given
backend. Accepted values (case-insensitive, whitespace trimmed):

============================================  ===============================
Value                                         Backend
============================================  ===============================
``rtls``, ``rustls``, ``aws-lc``, ``awslc``   Rustls + AWS-LC
``utls``, ``boringssl``                       BoringSSL
``ssl``, ``stdlib``                           Standard-library ``ssl``
============================================  ===============================

If the requested backend isn't installed, urllib3-future silently falls
through the remaining default order. Unknown values emit a warning and use the
default order.

.. code-block:: bash

    URLLIB3_FUTURE_SSL_BACKEND=ssl python my_script.py

.. versionadded:: 2.22.900

You can also use the ``ssl_backend`` kwargs into your Pool constructor and specify one of:

- ``rtls``
- ``utls``
- ``ssl``

This will overrule any default configuration and force a specific TLS backend.

An unavailable explicitly selected ``ssl_backend`` raises an error during
setup; it does not silently choose another backend. This setting selects the
TCP TLS implementation. HTTP/3 uses the QUIC/TLS implementation provided by
``qh3``.

The ``anytls`` module
~~~~~~~~~~~~~~~~~~~~~

The selection logic lives in :mod:`urllib3.contrib.anytls`. The module is
public and can be used to inspect the active backend or to perform
backend-agnostic ``except`` clauses:

.. code-block:: python

    from urllib3.contrib.anytls import (
        ssl,             # the active backend module
        stdlib_ssl,      # always the real stdlib ``ssl`` (or None)
        BACKEND,         # "rtls" | "utls" | "ssl" | "none"
        HAS_SSL,         # bool - True if any ssl backend is available
        IS_NONSTDLIB,    # bool - True for rtls or utls
        Certificate,     # peer certificate class
    )

    print(f"using {BACKEND} TLS backend ({ssl.OPENSSL_VERSION})")

Cross-backend ``SSLError``
~~~~~~~~~~~~~~~~~~~~~~~~~~

Both ``rtls.SSLError`` and ``utls.SSLError`` subclass ``ssl.SSLError`` from
the standard library, so ``except ssl.SSLError`` (and
``urllib3.exceptions.BaseSSLError``) keeps working unchanged regardless of
which backend is active.
