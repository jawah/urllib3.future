Troubleshooting
===============

Start by recording the library version, Python version, selected TLS backend,
and the protocol actually negotiated. A URL beginning with ``https://`` does
not by itself identify HTTP/1.1, HTTP/2, or HTTP/3.

.. code-block:: python
   :name: connection-diagnostics-example

   import sys
   import urllib3
   from urllib3.contrib.anytls import BACKEND, ssl


   def inspect_connection(info):
       print("HTTP:", info.http_version, "peer:", info.destination_address)
       print("TLS:", info.tls_version, "cipher:", info.cipher)
       print("ECH:", info.tls_ech_accepted)
       print("DNS:", info.resolution_latency, "connect:", info.established_latency)


   print("urllib3.future:", urllib3.__version__)
   print("Python:", sys.version)
   print("Default TCP TLS backend:", BACKEND, ssl.OPENSSL_VERSION)
   with urllib3.PoolManager(
       timeout=urllib3.Timeout(connect=5, read=10), retries=False
   ) as pm:
       response = pm.request("GET", sys.argv[1], on_post_connection=inspect_connection)
       print("Response:", response.status, response.version)

``BACKEND`` is the import-time default; an explicit pool-level ``ssl_backend``
can override it. HTTP/3 always uses qh3. The callback describes the connection
selected for this request, which may be reused rather than newly established.

.. _network-errors:

Understanding exceptions
------------------------

.. list-table:: Common failure categories
   :header-rows: 1
   :widths: 30 70

   * - Exception
     - What to investigate
   * - ``NameResolutionError``
     - Hostname resolution, resolver availability, and its timeout. System async lookup can also wait for an executor worker, even for an IP literal.
   * - ``NewConnectionError`` / ``ConnectTimeoutError``
     - The address, listening port, proxy route, firewall, and connect timeout.
   * - ``SSLError``
     - Certificate trust, hostname, client identity, and TLS negotiation. QUIC TLS alerts are also translated to this category.
   * - ``ReadTimeoutError``
     - Waiting for response data or extension input. This is not an overall download deadline.
   * - ``EmptyPoolError``
     - Connection acquisition and ``pool_timeout``. Check for responses left unread or unclosed and for exhausted stream capacity.
   * - ``ProtocolError``
     - HTTP framing, premature stream termination, HTTP/2 resets/GOAWAY, or HTTP/3 application errors. Inspect the message and chained exception.
   * - ``DecodeError``
     - Invalid or incomplete compressed response data.
   * - ``MaxRetryError``
     - The retry budget was exhausted. Inspect its ``reason`` for the final underlying failure.

The exact wrapper depends on retry policy and where the failure occurred.
For example, named protocol codes such as ``ENHANCE_YOUR_CALM`` identify the
peer's HTTP/2 error, while an HTTP/3 application close is distinct from a TLS
alert. An HTTP error status such as 503 is still a response unless a configured
status retry policy exhausts its budget and raises.

.. code-block:: python
   :name: retry-error-inspection-example

   import sys
   import urllib3
   from urllib3.exceptions import HTTPError, MaxRetryError

   with urllib3.PoolManager(timeout=urllib3.Timeout(connect=5, read=10)) as pm:
       try:
           response = pm.request("GET", sys.argv[1])
           print(response.status)
       except MaxRetryError as exc:
           print(type(exc.reason).__name__, exc.reason)
           raise
       except HTTPError as exc:
           print(type(exc).__name__, exc)
           raise

For a reproducible diagnostic request, ``retries=False`` avoids obscuring the
first failure with later attempts. It also disables redirect handling. Errors
can occur during ``get_response()`` or streaming after the initial request
call; catch them around the whole operation. Async callers should propagate
``asyncio.CancelledError`` after cleanup; see :ref:`async-cancellation`.

Why am I getting a different HTTP version?
------------------------------------------

Check ``response.version`` and :ref:`protocol-selection` before assuming a
negotiation failure:

* TCP uses ALPN to negotiate an allowed HTTP version. A plain HTTP endpoint
  needs :ref:`h2-prior-knowledge` when it requires HTTP/2 without TLS.
* HTTP/3 needs qh3, a supported platform, and a working UDP route. Discovery
  through Alt-Svc or HTTPS DNS records also depends on the server and resolver.
* A proxy or a WebSocket implementation can restrict the usable protocols.
  Plain WSS defaults to HTTP/1.1; opt into extended CONNECT with
  ``wss+rfc8441://`` when the server supports it.
* Active exchanges can delay an upgrade. Recoverable peer errors can trigger
  a downgrade and a new connection.

To isolate a QUIC-specific failure, repeat the diagnostic request with
``disabled_svn={urllib3.HttpVersion.h3}``. To isolate HTTP/1.1, disable both
``HttpVersion.h2`` and ``HttpVersion.h3``. Record the result as a comparison,
not as proof that the server or network supports the disabled protocols.

Keep TLS verification enabled while investigating protocol negotiation.
For private certificates, configure the appropriate trust store as described
in :ref:`custom_ssl_certificates`.

Logging and QUIC traces
-----------------------

Enable the library's debug logs without enabling debug output from every
dependency:

.. code-block:: python
   :name: debug-logging-example

   import logging

   logging.basicConfig(level=logging.INFO)
   logging.getLogger("urllib3").setLevel(logging.DEBUG)
   logging.getLogger("urllib3_future").setLevel(logging.DEBUG)

The logger namespace follows the imported package. The second name covers
the cohabitation installation. qh3 also uses the ``quic`` logger; enable its
debug level when investigating QUIC itself.

Set ``QUICLOGDIR`` to an existing directory to collect qlog files from HTTP/3
connections:

.. code-block:: bash

   mkdir -p quic-logs
   QUICLOGDIR="$PWD/quic-logs" python diagnose.py https://your-server.example/

Use the connection-diagnostics example as ``diagnose.py``. The request must
actually establish HTTP/3 for this trace to be produced. qlog records can be
opened with a compatible qlog viewer to inspect packet loss, timers, and
stream behavior. UDP GSO/GRO is an automatic optimization on supported
platforms; its fallback to individual datagrams is not an HTTP protocol error.

``SSLKEYLOGFILE`` can record TLS secrets for packet decryption where supported;
see :ref:`tls-key-logging`. Treat key logs as secrets and review URLs and
metadata before sharing logs. urllib3.future warns when these tracing
environment variables are enabled.
