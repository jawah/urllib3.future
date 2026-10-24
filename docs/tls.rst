TLS backends, ECH, and post-quantum security
============================================

urllib3.future uses the selected ``ssl``, ``rtls``, or ``utls`` backend for TLS
over TCP. HTTP/3 uses the TLS implementation inside ``qh3``. Installing a TCP
TLS backend does not replace the HTTP/3 engine.

See :ref:`tls-backends` for installation, selection order, and the
``ssl_backend`` setting. Certificate verification remains enabled by default
with every backend.

.. _tls-capabilities:

Choosing a backend
------------------

.. list-table::
   :header-rows: 1
   :widths: 20 25 25 30

   * - Implementation
     - HTTP transport
     - ECH integration
     - Additional behavior
   * - Standard-library ``ssl``
     - HTTP/1.1 and HTTP/2 over TCP
     - No ECH configuration API used by the current integration
     - TLS algorithms and options depend on the Python/OpenSSL or LibreSSL build.
   * - ``rtls`` (Rustls / AWS-LC)
     - HTTP/1.1 and HTTP/2 over TCP
     - Supported with HTTPS DNS records and a cooperating server
     - Algorithms and supported context options follow rtls and its cryptographic provider.
   * - ``utls`` (BoringSSL)
     - HTTP/1.1 and HTTP/2 over TCP
     - Supported with HTTPS DNS records and a cooperating server
     - Browser TLS profiles can also supply default HTTP request headers.
   * - ``qh3``
     - HTTP/3 over QUIC/UDP
     - Supported with HTTPS DNS records and a cooperating server
     - Provides its own TLS 1.3 negotiation, including hybrid post-quantum key exchange in supported versions.

Availability also depends on the platform and the installed dependency versions.
These capabilities describe what can be negotiated, not what every connection
will negotiate.

.. _ech:

Encrypted Client Hello (ECH)
----------------------------

ECH encrypts the inner TLS ClientHello, including the destination server name,
when the client and server can agree on an ECH configuration. It does not hide
the destination IP address. DNS transport encryption and DNSSEC validation are
separate properties; see :ref:`dnssec`.

urllib3.future can discover ECH configuration automatically through the HTTPS
records returned by its custom network resolvers. For TCP, install rtls or utls;
for QUIC, a compatible qh3 provides ECH. The default system resolver does not
supply the HTTPS-record metadata needed for this automatic discovery.

The following TCP example requires ``urllib3-future[rtls]``. Supply a URL whose
server publishes usable ECH configuration and supports ECH:

.. code-block:: python
   :name: ech-inspection-example

   import sys
   import urllib3


   def inspect_tls(info):
       print("HTTP:", info.http_version)
       print("TLS:", info.tls_version)
       print("ECH accepted:", info.tls_ech_accepted)


   with urllib3.PoolManager(
       ssl_backend="rtls",
       disabled_svn={urllib3.HttpVersion.h3},
       resolver="doh+cloudflare://",
       timeout=urllib3.Timeout(connect=5, read=10),
   ) as pm:
       response = pm.request("GET", sys.argv[1], on_post_connection=inspect_tls)
       print(response.status)

For HTTP/3, allow HTTP/3 and use a resolver that supplies HTTPS records; qh3
handles its TLS negotiation. For async requests, use ``AsyncPoolManager`` and
an ``async def`` callback as shown in :doc:`async`.

``ConnectionInfo.tls_ech_accepted`` reports acceptance on the selected
connection. ``True`` confirms ECH; ``False`` does not. ``None`` means that no
result is available on that information object. Missing or unusable DNS
metadata, backend support, or server support can prevent ECH. A successful
request alone does not prove that ECH was used.

Discovery is opportunistic: there is no public ``require_ech=True`` setting.
Observing the result in a callback is useful for diagnostics, but is not a
substitute for a transport policy that guarantees ECH before connecting.

Post-quantum key exchange
-------------------------

Supported qh3 versions offer hybrid key exchange such as X25519 combined with
ML-KEM-768. TCP capabilities depend on the selected TLS library and its
version. The server must also support the offered group; ordinary TLS
negotiation can select a classical group instead.

ECH and post-quantum key exchange solve different problems. ECH protects
ClientHello contents; hybrid key exchange strengthens the establishment of the
shared secret. Neither implies post-quantum certificate signatures.

``ConnectionInfo.cipher`` identifies the negotiated TLS cipher suite. A TLS 1.3
cipher suite does not identify the key-exchange group, so it cannot confirm
post-quantum negotiation. urllib3.future does not currently expose a uniform
negotiated-group field or a public setting requiring post-quantum key exchange.
Use the TLS backend's diagnostics when you need to verify that negotiation.

.. _utls-fingerprints:

utls profiles and request headers
---------------------------------

When constructing an unconstrained utls context, urllib3.future selects the
``chrome:stable`` profile. During HTTP/3 setup, a supplied utls context without
a profile can also acquire that default profile. A profile's paired HTTP
headers are merged into requests; explicitly supplied caller headers win,
including duplicate values represented by ``HTTPHeaderDict``.

This can change both the TLS ClientHello and default HTTP headers compared
with stdlib SSL. It does not turn the client into a browser, and a TCP
ClientHello profile does not control qh3's QUIC ClientHello.

To choose a profile explicitly, create a utls context before creating the pool:

.. code-block:: python
   :name: utls-profile-example

   import sys
   import utls
   import urllib3

   context = utls.SSLContext(utls.PROTOCOL_TLS_CLIENT)
   context.load_default_certs()
   context.set_fingerprint("chrome:stable")

   with urllib3.PoolManager(
       ssl_backend="utls",
       ssl_context=context,
       disabled_svn={urllib3.HttpVersion.h3},
       timeout=urllib3.Timeout(connect=5, read=10),
   ) as pm:
       response = pm.request(
           "GET", sys.argv[1], headers={"User-Agent": "my-application/1.0"}
       )
       print(response.status)

Available profiles and their algorithms are defined by the installed utls
version. For ordinary TLS without browser-profile defaults, explicitly select
``ssl_backend="ssl"`` or ``ssl_backend="rtls"``. Configure contexts before
sharing them between requests.

.. _tls-context-portability:

Custom contexts and HTTP/3
--------------------------

An ``ssl_context`` is used by the TCP TLS path. Conversion between stdlib and
an alternative backend is best effort; arbitrary callbacks, loaded identities,
and backend-specific options are not a portable configuration format. Use an
explicit backend and a context created for it when relying on those features.

For HTTP/3, urllib3.future extracts supported settings into qh3's configuration:

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Setting
     - HTTP/3 behavior
   * - CA files, directories, and PEM data
     - Passed to qh3. If none are supplied, accessible CA certificates can be read from the context or the default trust store.
   * - Certificate and hostname verification
     - The pool's verification policy and applicable context settings are used by the QUIC path. Keep verification enabled.
   * - Client certificates
     - Supply ``cert_file`` / ``key_file`` or ``cert_data`` / ``key_data`` explicitly. A client identity loaded only inside an SSLContext is not extracted for qh3.
   * - TLS version bounds
     - QUIC requires TLS 1.3. A maximum TLS version below 1.3 disables HTTP/3 eligibility.
   * - Ciphers, groups, ALPN, and browser profiles
     - An SSLContext is not passed through to qh3. Its TCP cipher or group configuration does not configure QUIC; qh3 negotiates HTTP/3 using its own TLS settings.

There is no public HTTP/3 cipher-selection parameter in urllib3.future.
``SSLContext.set_ciphers()`` should not be used as a way to constrain QUIC's
TLS 1.3 cipher suites. See :ref:`client-certificates` and :ref:`in-memory-mtls`
for explicit client identity configuration.
