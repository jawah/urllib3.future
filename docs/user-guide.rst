User Guide
==========

.. currentmodule:: urllib3

Installing
----------

urllib3 can be installed with `pip <https://pip.pypa.io>`_

.. code-block:: bash

  $ python -m pip install urllib3.future

This selects urllib3.future for ``import urllib3`` throughout the environment.
To keep upstream urllib3 and use the fork through ``import urllib3_future``,
follow the :doc:`cohabitation` guide.

HTTP/2 and HTTP/3 support
-------------------------

HTTP/2 support is enabled by default via the ``jh2`` dependency, HTTP/3 may or not be
automatically available depending on the availability of the wheel on your platform.

.. code-block:: bash

  $ python -m pip install qh3

This may require some external toolchain to be available (compilation).

.. note:: HTTP/3 is automatically installed and ready-to-use if you fulfill theses requirements: Linux, Windows or MacOS using Python (or PyPy) 3.7 onward with one of the supported architecture (aarch64/armv7l/s390x/x86_64/x86/i686/amd64/ppc64(le)).

.. caution:: If the requirements aren't fulfilled for HTTP/3, your package manager won't pick qh3 for installation when installing urllib3-future and it will be silently disabled. We choose not to impose compilation and keep a safe pure Python fallback.

.. note:: Very old ``pip`` versions may not be able to pick the pre-built wheel accordingly. Make sure to have the latest ``pip`` version installed first.

Making Requests
---------------

First things first, import the urllib3 module:

.. code-block:: python

    import urllib3

You'll need a :class:`~poolmanager.PoolManager` instance to make requests.
This object handles all of the details of connection pooling and thread safety
so that you don't have to:

.. code-block:: python

    http = urllib3.PoolManager()

To make a request use :meth:`~urllib3.PoolManager.request`:

.. code-block:: python

    import urllib3

    # Creating a PoolManager instance for sending requests.
    http = urllib3.PoolManager()

    # Sending a GET request and getting back response as HTTPResponse object.
    resp = http.request("GET", "https://httpbin.org/robots.txt")

    # Print the returned data.
    print(resp.data)
    # b"User-agent: *\nDisallow: /deny\n"

``request()`` returns a :class:`~response.HTTPResponse` object, the
:ref:`response_content` section explains how to handle various responses.

You can use :meth:`~urllib3.PoolManager.request` to make requests using any
HTTP verb:

.. code-block:: python

    import urllib3

    http = urllib3.PoolManager()
    resp = http.request(
        "POST",
        "https://httpbin.org/post",
        fields={"hello": "world"} #  Add custom form fields
    )

    print(resp.data)
    # b"{\n "form": {\n "hello": "world"\n  }, ... }

The :ref:`request_data` section covers sending other kinds of requests data,
including JSON, files, and binary data.

.. note:: For quick scripts and experiments you can also use a top-level ``urllib3.request()``.
    It uses a module-global ``PoolManager`` instance.
    Because of that, its side effects could be shared across dependencies relying on it.
    To avoid side effects, create a new ``PoolManager`` instance and use it instead.
    In addition, the method does not accept the low-level ``**urlopen_kw`` keyword arguments.
    System CA certificates are loaded on default.

.. _response_content:

Response Content
----------------

The :class:`~response.HTTPResponse` object provides
:attr:`~response.HTTPResponse.status`, :attr:`~response.HTTPResponse.data`, and
:attr:`~response.HTTPResponse.headers` attributes:

.. code-block:: python

    import urllib3

    # Making the request (The request function returns HTTPResponse object)
    resp = urllib3.request("GET", "https://httpbin.org/ip")

    print(resp.status)
    # 200
    print(resp.data)
    # b"{\n  "origin": "104.232.115.37"\n}\n"
    print(resp.headers)
    # HTTPHeaderDict({"Content-Length": "32", ...})
    print(resp.version)
    # 20

JSON Content
~~~~~~~~~~~~
JSON content can be loaded by :meth:`~response.HTTPResponse.json` 
method of the response:

.. code-block:: python

    import urllib3

    resp = urllib3.request("GET", "https://httpbin.org/ip")

    print(resp.json())
    # {"origin": "127.0.0.1"}

Alternatively, Custom JSON libraries such as `orjson` can be used to encode data,
retrieve data by decoding and deserializing the :attr:`~response.HTTPResponse.data` 
attribute of the request:

.. code-block:: python

    import orjson
    import urllib3

    encoded_data = orjson.dumps({"attribute": "value"})
    resp = urllib3.request(method="POST", url="http://httpbin.org/post", body=encoded_data)

    print(orjson.loads(resp.data)["json"])
    # {'attribute': 'value'}

Binary Content
~~~~~~~~~~~~~~

The :attr:`~response.HTTPResponse.data` attribute of the response is always set
to a byte string representing the response content:

.. code-block:: python

    import urllib3

    resp = urllib3.request("GET", "https://httpbin.org/bytes/8")

    print(resp.data)
    # b"\xaa\xa5H?\x95\xe9\x9b\x11"

.. note:: For larger responses, it's sometimes better to :ref:`stream <stream>`
    the response.

Using io Wrappers with Response Content
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Sometimes you want to use :class:`io.TextIOWrapper` or similar objects like a CSV reader
directly with :class:`~response.HTTPResponse` data. Making these two interfaces play nice
together requires using the :attr:`~response.HTTPResponse.auto_close` attribute by setting it
to ``False``. By default HTTP responses are closed after reading all bytes, this disables that behavior:

.. code-block:: python

    import io
    import urllib3

    resp = urllib3.request("GET", "https://example.com", preload_content=False)
    resp.auto_close = False

    for line in io.TextIOWrapper(resp):
        print(line)
    # <!doctype html>
    # <html>
    # <head>
    # ....
    # </body>
    # </html>

.. _request_data:

Request Data
------------

Headers
~~~~~~~

You can specify headers as a dictionary in the ``headers`` argument in :meth:`~urllib3.PoolManager.request`:

.. code-block:: python

    import urllib3

    resp = urllib3.request(
        "GET",
        "https://httpbin.org/headers",
        headers={
            "X-Something": "value"
        }
    )

    print(resp.json()["headers"])
    # {"X-Something": "value", ...}

Or you can use the ``HTTPHeaderDict`` class to create multi-valued HTTP headers:

.. code-block:: python

    import urllib3

    # Create an HTTPHeaderDict and add headers
    headers = urllib3.HTTPHeaderDict()
    headers.add("Accept", "application/json")
    headers.add("Accept", "text/plain")

    # Make the request using the headers
    resp = urllib3.request(
        "GET",
        "https://httpbin.org/headers",
        headers=headers
    )

    print(resp.json()["headers"])
    # {"Accept": "application/json, text/plain", ...}

Cookies
~~~~~~~

Cookies are specified using the ``Cookie`` header with a string containing
the ``;`` delimited key-value pairs:

.. code-block:: python

    import urllib3

    resp = urllib3.request(
        "GET",
        "https://httpbin.org/cookies",
        headers={
            "Cookie": "session=f3efe9db; id=30"
        }
    )

    print(resp.json())
    # {"cookies": {"id": "30", "session": "f3efe9db"}}  

Cookies provided by the server are stored in the ``Set-Cookie`` header:

.. code-block:: python

    import urllib3

    resp = urllib3.request(
        "GET",
        "https://httpbin.org/cookies/set/session/f3efe9db",
        redirect=False
    )

    print(resp.headers["Set-Cookie"])
    # session=f3efe9db; Path=/

Query Parameters
~~~~~~~~~~~~~~~~

For ``GET``, ``HEAD``, and ``DELETE`` requests, you can simply pass the
arguments as a dictionary in the ``fields`` argument to
:meth:`~urllib3.PoolManager.request`:

.. code-block:: python

    import urllib3

    resp = urllib3.request(
        "GET",
        "https://httpbin.org/get",
        fields={"arg": "value"}
    )

    print(resp.json()["args"])
    # {"arg": "value"}

For ``POST`` and ``PUT`` requests, you need to manually encode query parameters
in the URL:

.. code-block:: python

    from urllib.parse import urlencode
    import urllib3

    # Encode the args into url grammar.
    encoded_args = urlencode({"arg": "value"})

    # Create a URL with args encoded.
    url = "https://httpbin.org/post?" + encoded_args
    resp = urllib3.request("POST", url)

    print(resp.json()["args"])
    # {"arg": "value"}


.. _form_data:

Form Data
~~~~~~~~~

For ``PUT`` and ``POST`` requests, urllib3 will automatically form-encode the
dictionary in the ``fields`` argument provided to
:meth:`~urllib3.PoolManager.request`:

.. code-block:: python

    import urllib3

    resp = urllib3.request(
        "POST",
        "https://httpbin.org/post",
        fields={"field": "value"}
    )
    
    print(resp.json()["form"])
    # {"field": "value"}

.. _json:

JSON
~~~~

To send JSON in the body of a request, provide the data in the ``json`` argument to 
:meth:`~urllib3.PoolManager.request` and  urllib3 will automatically encode the data
using the ``json`` module with ``UTF-8`` encoding. 
In addition, when ``json`` is provided, the ``"Content-Type"`` in headers is set to 
``"application/json"`` if not specified otherwise.

.. code-block:: python

    import urllib3

    resp = urllib3.request(
        "POST",
        "https://httpbin.org/post",
        json={"attribute": "value"},
        headers={"Content-Type": "application/json"}
    )

    print(resp.json())
    # {'headers': {'Content-Type': 'application/json', ...}, 
    #  'data': '{"attribute":"value"}', 'json': {'attribute': 'value'}, ...}
    
Files & Binary Data
~~~~~~~~~~~~~~~~~~~

For uploading files using ``multipart/form-data`` encoding you can use the same
approach as :ref:`form_data` and specify the file field as a tuple of
``(file_name, file_data)``:

.. code-block:: python

    import urllib3

    # Reading the text file from local storage.
    with open("example.txt") as fp:
        file_data = fp.read()
    
    # Sending the request.
    resp = urllib3.request(
        "POST",
        "https://httpbin.org/post",
        fields={
           "filefield": ("example.txt", file_data),
        }
    )
    
    print(resp.json()["files"])
    # {"filefield": "..."}

While specifying the filename is not strictly required, it's recommended in
order to match browser behavior. You can also pass a third item in the tuple
to specify the file's MIME type explicitly:

.. code-block:: python

    resp = urllib3.request(
        "POST",
        "https://httpbin.org/post",
        fields={
            "filefield": ("example.txt", file_data, "text/plain"),
        }
    )

For sending raw binary data simply specify the ``body`` argument. It's also
recommended to set the ``Content-Type`` header:

.. code-block:: python

    import urllib3

    with open("/home/samad/example.jpg", "rb") as fp:
        binary_data = fp.read()

    resp = urllib3.request(
        "POST",
        "https://httpbin.org/post",
        body=binary_data,
        headers={"Content-Type": "image/jpeg"}
    )

    print(resp.json()["data"])
    # data:application/octet-stream;base64,...

.. _ssl:

WebSocket
---------

WebSocket over HTTP/1.1
~~~~~~~~~~~~~~~~~~~~~~~

Install ``urllib3-future[ws]`` to use the ``wsproto`` implementation. The
``wss://`` and ``ws://`` schemes select secure and plain WebSocket connections.
They use HTTP/1.1 by default.

.. code-block:: python
    :name: websocket-example

    import urllib3

    with urllib3.PoolManager() as pm:
        response = pm.urlopen("GET", "wss://echo.websocket.org", timeout=10)
        try:
            extension = response.extension
            if extension is None:
                raise RuntimeError(f"WebSocket was not accepted: {response.status}")
            extension.send_payload("Hello World!")
            message = extension.next_payload()
            if message is not None:
                print(message)
        finally:
            response.close()

A successful HTTP/1.1 upgrade returns status ``101``. Rejection can return an
ordinary HTTP response without an extension, and network or protocol failures
can raise an exception. ``next_payload()`` returns ``None`` at EOF. Servers can
send unsolicited messages, so the first message need not echo the last payload
you sent.

Full-duplex reads and writes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

For WebSocket over HTTP/1.1, a waiting ``next_payload()`` allows another thread
or task to call ``send_payload()`` or ``ping()``. Concurrent readers are
serialized so each message is delivered to one reader. Read timeouts remain
in effect while waiting for socket readiness and reacquiring the connection.
An already-entered blocking TLS receive can still delay a concurrent writer.

Alternative WebSocket engine
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Install ``urllib3-future[ws-fast]`` to use the ``websockets`` Sans-I/O
engine, then select ``ws+fast://`` or ``wss+fast://``. The extension
operations are also available through async extensions, where send, receive,
and close operations must be awaited.

Use the previous example with ``wss+fast://echo.websocket.org`` as the URL.
Keep the same acceptance check and ``finally`` cleanup.

This backend supports HTTP/1.1 and requires Python 3.9 or newer. Supported
``websockets`` versions range from 15.0.1 through 17.x: use 15.0.1 on Python
3.9, 16.1.1 on Python 3.10, and 17.1 on Python 3.11 or newer. Installing
``urllib3-future[ws-fast]`` selects a compatible version automatically.
When both engines are installed, plain ``ws://`` and ``wss://`` continue to
select ``wsproto``; use ``ws+fast://`` or ``wss+fast://`` to select this backend.

.. warning:: Versions 15.0.1 and 16.0 have an upstream DEBUG-log formatting bug for text frames
    that split a UTF-8 character. With logging handlers that propagate formatting
    exceptions, this can abort reception. Versions 16.1.1 and 17.1 pass this case.

WebSocket over HTTP/2 or HTTP/3
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Use ``ws+rfc8441://`` or ``wss+rfc8441://`` with a server that supports extended
CONNECT. This opt-in implementation uses ``wsproto`` and supports HTTP/2
(RFC8441), HTTP/3 (RFC9220), and HTTP/1.1 fallback. The ``fast`` implementation
is HTTP/1.1-only.

For example, replace the URL in the HTTP/1.1 example with
``wss+rfc8441://your-websocket-server.example/``. Successful extended CONNECT
uses a 2xx response, rather than HTTP/1.1's ``101`` status. Check
``response.extension`` and use the same cleanup pattern.

If you also pass ``multiplexed=True``, collect any returned promise before
accessing its extension; see :ref:`multiplexed-responses`. A plain WSS request
and an HTTPS request to the same origin can use different pools when their
protocol restrictions differ. See :ref:`pool-behavior`.

Certificate Verification
------------------------

.. note:: *New in version 1.25:*

    HTTPS connections are now verified by default (``cert_reqs = "CERT_REQUIRED"``).

While you can disable certification verification by setting ``cert_reqs = "CERT_NONE"``, it is highly recommend to leave it on.

Unless otherwise specified urllib3 will try to load the default system certificate stores.
The most reliable cross-platform method is to use the `certifi <https://certifi.io/>`_
package which provides Mozilla's root certificate bundle:

.. code-block:: bash

    $ python -m pip install certifi

Once you have certificates, you can create a :class:`~poolmanager.PoolManager`
that verifies certificates when making requests:

.. code-block:: python

    import certifi
    import urllib3

    http = urllib3.PoolManager(
        cert_reqs="CERT_REQUIRED",
        ca_certs=certifi.where()
    )

The :class:`~poolmanager.PoolManager` will automatically handle certificate
verification and will raise :class:`~exceptions.SSLError` if verification fails:

.. code-block:: python

    import certifi
    import urllib3

    http = urllib3.PoolManager(
        cert_reqs="CERT_REQUIRED",
        ca_certs=certifi.where()
    )

    http.request("GET", "https://httpbin.org/")
    # (No exception)

    http.request("GET", "https://expired.badssl.com", retries=False)
    # urllib3.exceptions.SSLError ...

.. note:: You can use OS-provided certificates if desired. Just specify the full
    path to the certificate bundle as the ``ca_certs`` argument instead of
    ``certifi.where()``. For example, most Linux systems store the certificates
    at ``/etc/ssl/certs/ca-certificates.crt``. Other operating systems can
    be `difficult <https://stackoverflow.com/questions/10095676/openssl-reasonable-default-for-trusted-ca-certificates>`_.

.. _timeout-boundaries:

Using Timeouts
--------------

Timeouts bound specific waits in a request. A float sets both the connect and
read timeout; it is not an overall deadline for the request, its retries, or
the entire response body. For example:

.. code-block:: python

    import urllib3

    resp = urllib3.request(
        "GET",
        "https://httpbin.org/delay/3",
        timeout=4.0
    )

    print(type(resp))
    # <class "urllib3.response.HTTPResponse">

    # This request will take more time to process than timeout.
    urllib3.request(
        "GET",
        "https://httpbin.org/delay/3",
        timeout=2.5
    )
    # MaxRetryError caused by ReadTimeoutError

For more granular control you can use a :class:`~util.timeout.Timeout`
instance which lets you specify separate connect and read timeouts:

.. code-block:: python

    import urllib3

    resp = urllib3.request(
        "GET",
        "https://httpbin.org/delay/3",
        timeout=urllib3.Timeout(connect=1.0)
    )

    print(type(resp))
    # <urllib3.response.HTTPResponse>

    urllib3.request(
        "GET",
        "https://httpbin.org/delay/3",
        timeout=urllib3.Timeout(connect=1.0, read=2.0)
    )
    # MaxRetryError caused by ReadTimeoutError


If you want all requests to be subject to the same timeout, you can specify
the timeout at the :class:`~urllib3.poolmanager.PoolManager` level:

.. code-block:: python

    import urllib3

    http = urllib3.PoolManager(timeout=3.0)
    
    http = urllib3.PoolManager(
        timeout=urllib3.Timeout(connect=1.0, read=2.0)
    )

``pool_timeout`` separately bounds waiting for a connection from a pool. DNS
resolvers can have their own budgets, and sequential address attempts,
redirects, retry delays, and application processing can extend the elapsed
time of an operation. ``Timeout(total=...)`` coordinates connect/read budgets;
it does not impose a wall-clock deadline across all those phases or all
streaming reads. A peer that keeps supplying data can keep a response alive
longer than the configured read timeout.

For cancellation limits in asynchronous code, see :ref:`async-cancellation`.
For native WASI timeout limitations, see :ref:`wasi-capabilities`.

You still override this pool-level timeout by specifying ``timeout`` to
:meth:`~urllib3.PoolManager.request`.

Keep-Alive
----------

.. note:: Available since urllib3-future v2.11 and before this only HTTP/1.1 were kept alive properly.

urllib3-future can automatically make sure that your HTTP connection is kept alive
no matter the used protocol using a discrete scheduled task for each host.

.. code-block:: python

    import urllib3

    http = urllib3.PoolManager(keepalive_delay=300, keepalive_idle_window=60)

In that example, we indicate that we wish to keep a connection alive for 5 minutes and
eventually send ping every 60s after the connection was idle. (Those values are the default ones!)

The pings are only sent when using HTTP/2 or HTTP/3 over QUIC. Any connection activity is considered as used, therefor
making the ping only 60s after zero activity. If the connection receive unsolicited data, it is also considered used.

.. note:: Setting either keepalive_delay or keepalive_idle_window to None disable this feature.

.. warning:: We do not recommend setting anything lower than 30s for keepalive_idle_window. Anything lower than 1s is considered to be 1s. High frequency ping will lower the performance of your connection pool.

Once the ``keepalive_delay`` passed, we do not close the connection, we simply cease to ensure it is alive. This is purely for backward compatibility with our predecessor, as some host may retain the connection for hours.

Retrying Requests
-----------------

urllib3 can automatically retry idempotent requests. This same mechanism also
handles redirects. You can control the retries using the ``retries`` parameter
to :meth:`~urllib3.PoolManager.request`. By default, urllib3 will retry
requests 3 times and follow up to 3 redirects.

To change the number of retries just specify an integer:

.. code-block:: python

    import urllib3

    urllib3.request("GET", "https://httpbin.org/ip", retries=10)

To disable all retry and redirect logic specify ``retries=False``:

.. code-block:: python

    import urllib3

    urllib3.request(
        "GET",
        "https://nxdomain.example.com",
        retries=False
    )
    # NewConnectionError

    resp = urllib3.request(
        "GET",
        "https://httpbin.org/redirect/1",
        retries=False
    )

    print(resp.status)
    # 302

To disable redirects but keep the retrying logic, specify ``redirect=False``:

.. code-block:: python

    resp = urllib3.request(
        "GET",
        "https://httpbin.org/redirect/1",
        redirect=False
    )
    
    print(resp.status)
    # 302

For more granular control you can use a :class:`~util.retry.Retry` instance.
This class allows you far greater control of how requests are retried.

For example, to do a total of 3 retries, but limit to only 2 redirects:

.. code-block:: python

    urllib3.request(
        "GET",
        "https://httpbin.org/redirect/3",
        retries=urllib3.Retry(3, redirect=2)
    )
    # MaxRetryError

You can also disable exceptions for too many redirects and just return the
``302`` response:

.. code-block:: python

    resp = urllib3.request(
        "GET",
        "https://httpbin.org/redirect/3",
        retries=urllib3.Retry(
            redirect=2,
            raise_on_redirect=False
        )
    )
    
    print(resp.status)
    # 302

If you want all requests to be subject to the same retry policy, you can
specify the retry at the :class:`~urllib3.poolmanager.PoolManager` level:

.. code-block:: python

    import urllib3

    http = urllib3.PoolManager(retries=False)

    http = urllib3.PoolManager(
        retries=urllib3.Retry(5, redirect=2)
    )

You still override this pool-level retry policy by specifying ``retries`` to
:meth:`~urllib3.PoolManager.request`.

Inspecting response bodies in retry hooks
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Set ``Retry(cache_response_body=True)`` to retain a response body before retry-delay
hooks run, including when the request uses ``preload_content=False``. This is disabled
by default in both sync and async. Retry eligibility still depends on the configured
status codes and methods; enabling caching does not make additional responses retryable.

For synchronous requests, override ``get_retry_after()`` and inspect ``response.data``
or ``response.json()``. For asynchronous requests, override ``async_get_retry_after()``
and await those accessors. For example, a service might return a JSON ``retry_delay``:

.. code-block:: python

    class BodyRetry(urllib3.Retry):
        async def async_get_retry_after(self, response):
            delay = await super().async_get_retry_after(response)
            if delay is not None:
                return delay
            body = await response.json()
            return min(self.retry_after_max, max(0, float(body["retry_delay"])))

    async with urllib3.AsyncPoolManager() as pm:
        response = await pm.request(
            "GET", "https://example.com/api",
            retries=BodyRetry(
                total=2, status_forcelist=[503], cache_response_body=True
            ),
            preload_content=False,
        )

The entire retry body is loaded into memory. Decompression follows the response's
``decode_content`` setting, and compressed bodies can expand substantially. Body read
and decoding errors propagate. The connection is released before retry-delay hooks
and sleeping; final responses remain available to the caller. The async hook defaults
to the existing synchronous hook, preserving overrides that only inspect headers.

Errors & Exceptions
-------------------

urllib3 wraps lower-level exceptions, for example:

.. code-block:: python

    import urllib3

    try:
        urllib3.request("GET","https://nx.example.com", retries=False)

    except urllib3.exceptions.NewConnectionError:
        print("Connection failed.")
    # Connection failed.

See :mod:`~urllib3.exceptions` for the full list of all exceptions.

Logging
-------

If you are using the standard library :mod:`logging` module urllib3 will
emit several logs. In some cases this can be undesirable. You can use the
standard logger interface to change the log level for urllib3's logger:

.. code-block:: python

    logging.getLogger("urllib3").setLevel(logging.WARNING)
