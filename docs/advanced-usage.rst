Advanced Usage
==============

.. currentmodule:: urllib3


Customizing Pool Behavior
-------------------------

The :class:`~poolmanager.PoolManager` class automatically handles creating
:class:`~connectionpool.ConnectionPool` instances for each host as needed. By
default, it will keep a maximum of 10 :class:`~connectionpool.ConnectionPool`
instances. If you're making requests to many different hosts it might improve
performance to increase this number.

.. code-block:: python

    import urllib3

    http = urllib3.PoolManager(num_pools=50)

However, keep in mind that this does increase memory and socket consumption.

Similarly, the :class:`~connectionpool.ConnectionPool` class keeps a pool
of individual :class:`~connection.HTTPConnection` instances. These connections
are used during an individual request and returned to the pool when the request
is complete. By default only one connection will be saved for re-use. If you
are making many requests to the same host simultaneously it might improve
performance to increase this number.

.. code-block:: python

    import urllib3

    http = urllib3.PoolManager(maxsize=10)
    # Alternatively
    pool = urllib3.HTTPConnectionPool("google.com", maxsize=10)

The behavior of the pooling for :class:`~connectionpool.ConnectionPool` is
different from :class:`~poolmanager.PoolManager`. By default, if a new
request is made and there is no free connection in the pool then a new
connection will be created. However, this connection will not be saved if more
than ``maxsize`` connections exist. This means that ``maxsize`` does not
determine the maximum number of connections that can be open to a particular
host, just the maximum number of connections to keep in the pool. However, if you specify ``block=True`` then there can be at most ``maxsize`` connections
open to a particular host.

.. code-block:: python

    http = urllib3.PoolManager(maxsize=10, block=True)

    # Alternatively
    pool = urllib3.HTTPConnectionPool("google.com", maxsize=10, block=True)

Any new requests will block until a connection is available from the pool.
This is a great way to prevent flooding a host with too many connections in
multi-threaded applications.

.. _stream:
.. _streaming_and_io:

Streaming and I/O
-----------------

When using ``preload_content=True`` (the default setting) the
response body will be read immediately into memory and the HTTP connection
will be released back into the pool without manual intervention.

However, when dealing with large responses it's often better to stream the response
content using ``preload_content=False``. Setting ``preload_content`` to ``False`` means
that urllib3 will only read from the socket when data is requested.

.. note:: When using ``preload_content=False``, you need to manually release
    the HTTP connection back to the connection pool so that it can be re-used.
    To ensure the HTTP connection is in a valid state before being re-used
    all data should be read off the wire.

    You can call the  :meth:`~response.HTTPResponse.drain_conn` to throw away
    unread data still on the wire. This call isn't necessary if data has already
    been completely read from the response.

    After all data is read you can call :meth:`~response.HTTPResponse.release_conn`
    to release the connection into the pool.

    You can call the :meth:`~response.HTTPResponse.close` to close the connection,
    but this call doesn’t return the connection to the pool, throws away the unread
    data on the wire, and leaves the connection in an undefined protocol state.
    This is desirable if you prefer not reading data from the socket to re-using the
    HTTP connection.

:meth:`~response.HTTPResponse.stream` lets you iterate over chunks of the response content.

.. code-block:: python

    import urllib3

    resp = urllib3.request(
        "GET",
        "https://httpbin.org/bytes/1024",
        preload_content=False
    )

    for chunk in resp.stream(32):
        print(chunk)
        # b"\x9e\xa97'\x8e\x1eT ....

    resp.release_conn()

If you desire to iterate over chunks as soon as they arrive, specify ``-1`` as the **amt**.

.. code-block:: python

    import urllib3

    resp = urllib3.request(
        "GET",
        "https://httpbin.org/bytes/1024",
        preload_content=False
    )

    for chunk in resp.stream(-1):
        print(chunk)
        # b"\x9e\xa97'\x8e\x1eT ....

    resp.release_conn()

However, you can also treat the :class:`~response.HTTPResponse` instance as
a file-like object. This allows you to do buffering:

.. code-block:: python

    import urllib3

    resp = urllib3.request(
        "GET",
        "https://httpbin.org/bytes/1024",
        preload_content=False
    )

    print(resp.read(4))
    # b"\x88\x1f\x8b\xe5"

Calls to :meth:`~response.HTTPResponse.read()` will block until more response
data is available.

.. code-block:: python

    import io
    import urllib3

    resp = urllib3.request(
        "GET",
        "https://httpbin.org/bytes/1024",
        preload_content=False
    )

    reader = io.BufferedReader(resp, 8)
    print(reader.read(4))
    # b"\xbf\x9c\xd6"

    resp.release_conn()

You can use this file-like object to do things like decode the content using
:mod:`codecs`:

.. code-block:: python

    import codecs
    import json
    import urllib3

    reader = codecs.getreader("utf-8")

    resp = urllib3.request(
        "GET",
        "https://httpbin.org/ip",
        preload_content=False
    )

    print(json.load(reader(resp)))
    # {"origin": "127.0.0.1"}

    resp.release_conn()

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

    ctx = create_urllib3_context()
    ctx.load_default_certs()
    ctx.options |= ssl.OP_ENABLE_MIDDLEBOX_COMPAT

    with PoolManager(ssl_context=ctx) as pool:
        pool.request("GET", "https://www.google.com/")

Note that this is different from passing an ``options`` argument to
:func:`~urllib3.util.create_urllib3_context` because we don't overwrite
the default options: we only add a new one.

Remembering HTTP/3 over QUIC support
------------------------------------

There is a chance that you may want to speed up HTTP/3 negotiation. urllib3 does
not remember if a particular host, port HTTP server is capable of serving QUIC.

In practice, we always have to initiate a TCP connection and then observe the first response headers
in order to determine if the remote is capable of communicating through QUIC.

.. note:: Since urllib3.future 2.4+ we are capable of asking for DNS HTTPS records to preemptively connect using HTTP/3 over QUIC.

.. note::

    HTTP/3 require installing ``qh3`` package if not automatically grabbed.

.. code-block:: python

    from urllib3 import PoolManager

    quic_cache = dict()

    with PoolManager(preemptive_quic_cache=quic_cache) as pool:
        pool.request("GET", "https://www.cloudflare.com")

In bellow example, the variable ``quic_cache`` will be populated with a single entry and
if you pickle and restore this variable in between interpreter run, it should not make TCP connection
prior to the QUIC one.

urllib3 is meant to be thread safe, so we do not provide any 'default' solution for the caching. It
is up to you.

``preemptive_quic_cache`` takes any ``MutableMapping[Tuple[str, int], Tuple[str, int] | None]``.

Note that to lower the attack surface we won't allow hostname switching from saved Alt-Svc entry.

Explicitly disable HTTP/2 and/or HTTP/3
---------------------------------------

You can, at your own discretion, disable HTTP/2 and/or HTTP/3 by passing the argument ``disabled_svn``
to your ``PoolManager``.
It takes a ``set`` of ``HttpVersion`` like so:

.. code-block:: python

    from urllib3 import PoolManager, HttpVersion

    with PoolManager(disabled_svn={HttpVersion.h3, HttpVersion.h2}) as pool:
        resp = pool.request("GET", "https://www.cloudflare.com")
        print(resp.version)  # 11

.. note::

    HTTP/3 require installing ``qh3`` package if not automatically available. Setting disabled_svn has no effect otherwise.
    Also, you cannot disable HTTP/1.1 at the current state of affairs.

Multiplexed connection
----------------------

Since the version 2.2 you can emit multiple concurrent requests and retrieve the responses later.
A new keyword argument is available in ``PoolManager``, ``HTTPPoolConnection`` through the following methods:

- :meth:`~urllib3.PoolManager.request`
- :meth:`~urllib3.PoolManager.urlopen`
- :meth:`~urllib3.PoolManager.request_encode_url`
- :meth:`~urllib3.PoolManager.request_encode_body`

When you omit ``multiplexed=...`` it default to the old behavior of waiting upon the response and return a :class:`~urllib3.response.HTTPResponse`
otherwise if you specify ``multiplexed=True`` it will return a :class:`~urllib3.backend.ResponsePromise` instead.

Here is an example::

    from urllib3 import PoolManager

    with PoolManager() as pm:
        promise0 = pm.urlopen("GET", "https://pie.dev/delay/3", multiplexed=True)
        # <ResponsePromise 'IOYTFooi0bCuaQ9mwl4HaA==' HTTP/2.0 Stream[1]>
        promise1 = pm.urlopen("GET", "https://pie.dev/delay/1", multiplexed=True)
        # <ResponsePromise 'U9xT9dPVGnozL4wzDbaA3w==' HTTP/2.0 Stream[3]>
        response0 = pm.get_response()
        # the second request arrived first
        response0.json()["url"]  # https://pie.dev/delay/1
        # the first arrived last
        response1 = pm.get_response()
        response1.json()["url"]  # https://pie.dev/delay/3

  or you may do::

    from urllib3 import PoolManager

    with PoolManager() as pm:
        promise0 = pm.urlopen("GET", "https://pie.dev/delay/3", multiplexed=True)
        # <ResponsePromise 'IOYTFooi0bCuaQ9mwl4HaA==' HTTP/2.0 Stream[1]>
        promise1 = pm.urlopen("GET", "https://pie.dev/delay/1", multiplexed=True)
        # <ResponsePromise 'U9xT9dPVGnozL4wzDbaA3w==' HTTP/2.0 Stream[3]>
        response0 = pm.get_response(promise=promise0)
        # forcing retrieving promise0
        response0.json()["url"]  # https://pie.dev/delay/3
        # then pick first available
        response1 = pm.get_response()
        response1.json()["url"]  # https://pie.dev/delay/1

.. note:: You cannot expect the connection upgrade to HTTP/3 if all in-flight request aren't consumed.

.. warning:: Using ``multiplexed=True`` if the target connection does not support it is ignored and assume you meant ``multiplexed=False``. It will raise a warning in a future version.

Associate a promise to its response
-----------------------------------

When issuing concurrent request using ``multiplexed=True`` and want to retrieve
the responses in whatever order they may come, you may want to clearly identify the originating promise.

To identify with certainty::

    from urllib3 import PoolManager

    with PoolManager() as pm:
        promise0 = pm.urlopen("GET", "https://pie.dev/delay/3", multiplexed=True)
        # <ResponsePromise 'IOYTFooi0bCuaQ9mwl4HaA==' HTTP/2.0 Stream[1]>
        promise1 = pm.urlopen("GET", "https://pie.dev/delay/1", multiplexed=True)
        # <ResponsePromise 'U9xT9dPVGnozL4wzDbaA3w==' HTTP/2.0 Stream[3]>
        response = pm.get_response()
        # verify that response is linked to second promise
        response.is_from_promise(promise0)
        # True!
        response.is_from_promise(promise1)
        # False.

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

.. note:: If your platform isn't served by this feature it will raise a warning and ignore the certificate.

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
        resp = pm.urlopen("POST", "https://httpbin.org/post", data=b"foo"*1024*10, on_upload_body=track)

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

DNS over UDP (Insecure)
~~~~~~~~~~~~~~~~~~~~~~~

In order to specify your own DNS server over UDP you can specify it like so::

    from urllib3 import PoolManager

    with PoolManager(resolver="dou://1.1.1.1") as pm:
        pm.urlopen(...)

You can pass the following options to the DNS url:

- timeout
   - ``dou://1.1.1.1/?timeout=1.2``
- source_address
   - ``dou://1.1.1.1/?source_address=10.12.0.1:1111``

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

.. note:: Beware that we automatically forward ``ca_cert_data``, `ca_cert_dir`, and ``ca_certs`` (if specified) for convenience if not specified in DNS parameters.

When passing the resolver as a plain string url, you can do as follow::

    from urllib3 import PoolManager

    with PoolManager(resolver="doh+google://default?timeout=2&cert_reqs=0") as pm:
        pm.urlopen(...)

.. note:: The following set the timeout to 2 and disable the certificate verification.

It is also possible to pass more complex argument like **ca_cert_data** using a ``ResolverDescription`` instance::

    from urllib3 import PoolManager, ResolverDescription
    import wassima

    my_resolver = ResolverDescription.from_url("doh+google://default?timeout=2&cert_reqs=0")
    my_resolver["ca_cert_data"] = wassima.generate_ca_bundle()

    with PoolManager(resolver="doh+google://default?timeout=2") as pm:
        pm.urlopen(...)

.. note:: That example showcase how to inject your OS trust store CA to be used with the DNS connection verification.

Create your own Resolver
------------------------

You can create a resolver from scratch by inheriting ``BaseResolver`` that is located at ``urllib3.contrib.resolver``.
Then, once ready, you can instantiate it and pass it directly into the keyword argument ``resolver=...``.

The minimum viable resolver requires you to implement the methods ``getaddrinfo(...)``, ``close()``, and ``is_available()``.

Use cases:

- DNS over PostgreSQL (Using a database to translate hostnames)
- DNS over Redis (Implementing a sharable persistent cache)

.. note:: You can inherit any of ``urllib3.contrib.dot.TLSResolver``, ``urllib3.contrib.doq.QUICResolver``, ``urllib3.contrib.dou.PlainResolver``, or ``urllib3.contrib.doh.HTTPSResolver`` and add your own layer. e.g. the redis sharable cache layer.

DNSSEC
------

When you leverage a DNS resolver that is not the default, meaning DNS over QUIC / TLS / HTTPS and UDP, that ships
within urllib3.future native capabilities you should expect DNSSEC to be enforced. See https://www.cloudflare.com/learning/dns/dns-security/ for
in-depth explanations on the matter.

You can execute the following code to witness it::

    from urllib3 import PoolManager

    with PoolManager(resolver="doh+cloudflare://") as pm:
        pm.urlopen("GET", "https://brokendnssec.net")

This will raise an exception with the following message::

     Failed to resolve 'brokendnssec.net' (DNSSEC validation failure. Check http://dnsviz.net/d/brokendnssec.net/dnssec/ and http://dnssec-debugger.verisignlabs.com/brokendnssec.net for errors)

.. note:: You cannot circumvent that security check. It may be a life saver to you or your company. If you really want this feature shutdown, use `resolver=None`. You won't be able to support (secure) alternative DNS providers.

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
        pm.urlopen("GET, "https://pie.dev/get", on_post_connection=lambda ci: print(ci))

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

    async with urllib3.AsyncPoolManager(happy_eyeballs=True) as pm:
        ...

.. note:: The keyword argument ``happy_eyeballs`` can be used in ``AsyncPoolManager``, ``AsyncHTTPConnectionPool`` and its synchronous counterparts.

This feature works out-of-the-box no matter what protocol you are using or interpreter version.

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

    async with urllib3.AsyncPoolManager(happy_eyeballs=10) as pm:
        ...

This will enable up to 10 concurrent connections. To be clear, with that setting,
if your DNS resolver yield 6 addresses, you will spawn 6 tasks.

.. warning:: Setting more than 20 is impracticable, DNS servers have a set limit of how many records can be returned. Most of the time, regular user are advised to leave the default value.

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

Fastly provide a test server that can helps you see that feature in action::

    from urllib3 import PoolManager, HTTPResponse

    early_response: HTTPResponse | None = None

    def my_callback(caught_response: HTTPResponse) -> None:
        nonlocal early_response
        early_response = caught_response

    with PoolManager() as pm:
        response = pm.urlopen("GET", "https://early-hints.fastlylabs.com/", on_early_response=my_callback)

    print(early_response.status)  # 103
    print(early_response.headers) # HTTPHeaderDict({"Link": "...
    print(response.status)  # 200

This example works whether you enable manual multiplexing or using asyncio.

.. warning:: Some webservers may enable that feature on HTTP/2+ and disable it in HTTP/1.1. In urllib3-future case, that feature is available no matter the used protocol.

.. note:: The status 101 (Switching Protocol) is never considered "early", therefor "response" will be the 101 one and "early_response" will be worth "None".

.. note:: Any responses yielded through the "on_early_response" callback will never have a body per standards.

Switching Protocol
------------------

.. note:: Available since urllib3-future version 2.10 or greater.

Manually passing from HTTP to a sub protocol can be achieved easily thanks to our ``DirectStreamAccess`` policy.
If, for any reason, you wanted to negotiate ``WebSocket`` manually or any other proprietary protocol after receiving
a ``101 Switching Protocol`` response or alike; you may access the RawExtensionFromHTTP that is available on your
response object.

.. code-block:: python

    import urllib3

    with urllib3.PoolManager() as pm:
        resp = pm.urlopen("GET", "https://example.tld", headers={...})

        print(resp.status)  # output '101' for 'Switching Protocol' response status

        print(resp.extension)  # output <class 'urllib3.contrib.webextensions.RawExtensionFromHTTP'>

        print(resp.extension.next_payload())  # read from the stream
        resp.extension.send_payload(b"..")  # write in the stream

        # gracefully close the sub protocol.
        resp.extension.close()


.. note:: The important thing here, is that, when the server agrees to stop speaking HTTP in favor of something else, the ``response.extension`` is set and you will be able to exchange raw data at will.

.. warning:: In HTTP/2 or HTTP/3 you want to replace ``"GET"`` by ``"CONNECT"`` and add a header named ``:protocol`` to issue a proper "Upgrade".

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

Server Side Event
-----------------

.. note:: Upgrade urllib3-future to 2.12+ or later to benefit from this.

You can start leveraging SSE through HTTP/1, HTTP/2 or HTTP/3 with a mere line of code!
Everything is handled by us, you don't have to worry about protocols internal. Do as you
did simple http request.

.. code-block:: python

    import urllib3

    if __name__ == "__main__":

        with urllib3.PoolManager() as pm:
            r = pm.urlopen("GET", "sse://httpbingo.org/sse?delay=1s&duration=5s&count=10")

            while r.extension.closed is False:
                event = r.extension.next_payload()  # here, next_payload() returns an ServerSentEvent object.
                print(event)
                print(event.json())

            print(r.trailers)  # some server can give you some stats post ending the stream of events, you'll find them here.

.. warning:: By default ``sse://`` is bound to ``https://``. Use ``psse://`` to connect using plain http instead.

In opposition to WebSocket, the method ``next_payload()`` output an object. The event fully parsed.
If you wanted to get the raw event, untouched, as unicode string, add the kwarg ``raw=True`` into the method ``next_payload()``.

Debug your pool state
---------------------

Ever wondered what is inside your ``PoolManager`` or ``AsyncPoolManager``?
Wonder no more! starting from urllib3-future 2.12.913+ you can have a proper representation of those objects.

.. code-block:: python

    import urllib3

    if __name__ == "__main__":

        with urllib3.PoolManager() as pm:
            r = pm.urlopen("GET", "https://httpbingo.org/get)
            print(pm)  # <PoolManager <HTTPSConnectionPool "httpbingo.org:443" <TrafficPolice 1/10 (Idle)>> <TrafficPolice 1/10 (Idle)>>

.. note:: TrafficPolice is the inner task/thread safety container. The representation is as follow: used/maxsize (status).

.. note:: TrafficPolice is either 'idle', 'used' or 'saturated'.

.. warning:: Both ``PoolManager`` and ``ConnectionPool`` have a ``TrafficPolice`` inside them.

.. danger:: Calling ``repr`` here is highly discouraged at a high frequency. Do it for debug purposes only.
