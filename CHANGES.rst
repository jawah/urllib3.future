2.11.905 (2024-10-26)
=====================

- Fixed custom loop like uvloop needing advanced error handling on transport close.
- Fixed MacOS connection reset by peer handling to detect connection close (continuation of fix in 2.11.902)

2.11.904 (2024-10-25)
=====================

- Improve (async) close procedure when used in a ``uvloop``.

2.11.903 (2024-10-22)
=====================

- Fixed (low-level) exception leak when using ``get_response(...)`` after ``urlopen(..., multiplexed=True)``.
- Fixed erroneous calculated maximal wait when starting a connection upgrade to a higher protocol version in rare cases (async+windows only).

2.11.902 (2024-10-22)
=====================

- Added viable replacement for connection close detection since we stopped using the function ``wait_for_read``
  in property ``is_connected`` of a ``HTTPConnection`` object. And we harmonized the behavior whether you use async
  or sync.

2.11.901 (2024-10-21)
=====================

- Fixed error in ``is_connected`` for a Connection. The logic is no longer applicable due to how urllib3-future grows.
  We no longer use the function ``wait_for_read``. Also we stopped using MSG_PEEK for our discrete incoming data watcher
  due to suspicious behavior noticed. Finally we shielded any exception from attempting to close a broken socket.

2.11.900 (2024-10-21)
=====================

- Added a discrete task for each instantiated ``ConnectionPool`` to watch for unsolicited incoming data.
  This improves the fix shipped in v2.10.906 and avoid having to recycle your multiplexed connection in idle moments.
  A new keyword argument is supported in your PoolManager configuration, namely ``background_watch_delay``.
  This parameter takes a int or float as the delay between checks. Set it to None to void this background task.
  Anything lower than ``0.01`` will be interpreted as None, therefor disabling the discrete watch.
- Added managed keepalive for HTTP/2 and HTTP/3 over QUIC. A new keyword argument, named ``keepalive_delay`` that
  takes a value expressed in seconds for how long urllib3-future should automatically keep the connection alive.
  This is done in direct extension to our "discrete task" mentioned just before. We will send ``PING`` frame
  automatically to the remote peer every 60s by default (after idle for 60s to be clear). The window delay for
  sending a ``PING`` is configurable via the ``keepalive_idle_window`` parameter. Learn more about this in our
  documentation.
- Fixed evaluation of ``fp`` in our ``LowLevelResponse`` instance to raise ``AttributeError`` when it cannot be
  accessed. This will help with ``cachecontrol[filecache]`` way of determining if response was consumed entirely.

2.10.906 (2024-10-17)
=====================

- Fixed handling aggressive ACKs watcher in some QUIC server implementation leading to a ``ProtocolError``.
  We're actively working toward a solution that will avoid to recycle the QUIC connection.

2.10.905 (2024-10-15)
=====================

- Fixed dangling task waiting for timeout when using Happy Eyeballs in a synchronous context.

2.10.904 (2024-10-13)
=====================

- Fixed thread/task safety with WebSocket R/W operations.
- Fixed missing propagation of callbacks (e.g. ``on_post_connection``) in retries of failed requests.

2.10.903 (2024-10-12)
=====================

- Fixed exception leaks in ExtensionFromHTTP plugins. Now every extension behave and raise urllib3 own exceptions.
- Added automatic connection downgrade HTTP/2 -> HTTP/1.1 or HTTP/3 -> (HTTP/2 or HTTP/1.1) in case of known recoverable issues.
- Fixed a rare issue where the write semaphore (async context) for a datagram socket would be locked forever in case of an error.

2.10.902 (2024-10-09)
=====================

- Fixed call to ``stream(..)`` on (early) informational responses. The inner ``fp`` was set to ``None`` and the function
  ``is_fp_closed`` is not meant to handle this case. Through you should never expect a body in those responses.
- Fixed ``read()``, and ``data`` returns None for (early) informational responses.

2.10.901 (2024-10-08)
=====================

- Fixed closed state on a WebSocketExtensionFromHTTP when the remote send a CloseConnection event.
- Fixed an edge case where a DNS-over-HTTPS would start of a non-multiplexed connection but immediately upgrade to a
  multiplexed capable connection would induce an error.
- Allow to disable HTTP/1.1 in a DNS-over-HTTPS resolver.
- Extra "qh3" lower bound aligned with the main constraint ``>=1.2,<2``.

2.10.900 (2024-10-06)
=====================

- Added complete support for Informational Response whether it's an early response or not. We introduced a callback named
  ``on_early_response`` that takes exactly one parameter, namely a ``HTTPResponse``. You may start leveraging Early Hints!
  This works regardless of the negotiated protocol: HTTP/1.1, HTTP/2 or HTTP/3! As always, you may use that feature
  in a synchronous or asynchronous context.
- Changed ``qh3`` lower bound version to v1.2 in order to support Informational Response in HTTP/3 also.
- Added full automated support for WebSocket through HTTP/1.1, HTTP/2 or HTTP/3.
  In order to leverage this feature, urllib3-future now recognize url scheme ``ws://`` (insecure) and ``wss://`` (secure).
  The response will be of status 101 (Switching Protocol) and the body will be None.
  Most servers out there only support WebSocket through HTTP/1.1, and using HTTP/2 or HTTP/3 usually ends up in stream (reset) error.
  By default, connecting to ``wss://`` or ``ws://`` use HTTP/1.1, but if you desire to leverage the WebSocket through a multiplexed connection,
  use ``wss+rfc8441://`` or ``ws+rfc8441://``.
  A new property has been introduced in ``HTTPResponse``, namely ``extension`` to be able to interact with the websocket
  server. Everything is handled automatically, from thread safety to all the protocol logic. See the documentation for more.
  This will require the installation of an optional dependency ``wsproto``, to do so, please install urllib3-future with
  ``pip install urllib3-future[ws]``.
- Fixed a rare issue where the ``:authority`` (special header) value might be malformed.

2.9.900 (2024-09-24)
====================

- Fixed a rare issue where HTTPS record is misinterpreted, thus leading to a missed preemptive HTTP/3 negotiation.
- Restored support for older-and-deprecated ``PySocks`` if installed and ``python-socks`` is absent for synchronous support of SOCKS proxies.
- Added support for HTTP Trailers across HTTP/1, HTTP/2 and HTTP/3 responses. We added the property ``trailers`` in ``HTTPResponse`` to reflect that.
- Fixed unclosed resource warning for socket created in asynchronous mode.
- Added support for Upgrading to HTTP/2 (If coming from HTTP/1) via Alt-Svc. Whether it's h2c (http/2 over cleartext) or h2.
- Largely improve download speed on the QUIC layer by increasing automatically the blocksize to the largest value allowed on UDP (value taken from sysconf).
- Fixed the test suite outcome if no support for HTTP/3 exist in current environment.

2.8.907 (2024-08-20)
====================

- Fixed http2 maximum frame size error when the remote explicitly set a lower value than the default blocksize.
  This can happen when facing an Apache (httpd) server see https://github.com/apache/httpd/commit/ff6b8026acb8610e4faf10ee345141a3da85946e
  Now we monitor the max_frame setting value to ensure we don't exceed it.

2.8.906 (2024-08-15)
====================

- Removed opinionated OpenSSL version constraint that forbid any version lower than 1.1.1.
  The reasoning behind this is that some companies expressed (to us) the need to upgrade urllib3 to urllib3-future
  in (very) old Python 3.7 built against patched OpenSSL 1.0.2 or 1.0.8 and collaborative testing showed us
  that this constraint is overly protective. Those build often lack TLS 1.3 support and may contain
  major vulnerabilities, but we have to be optimistic on their awareness.
  TLS 1.3 / QUIC is also an option for them as it works out of the box on those old distributions.
  Effective immediately, we added a dedicated pipeline in our CI to verify that urllib3-future works
  with the oldest Python 3.7 build we found out there.
  Blindly removing support for those libraries when supporting Python 3.7 ... 3.9 is as we "partially"
  support this range and end-users have no to little clues for why it's rejected when it clearly works.
  The only issue that can appear is for users that have Python built against a SSL library that does not
  support either TLS 1.2 or 1.3, they will encounter errors for sure.
- Changed to submodule http2 to subpackage http2. Purely upstream sync. Still no use for us.
- Changed minimum (C)Python interpreter version for qh3 automatic pickup to 3.7.11 as it bundle pip 21.2.4 and
  is the minimum version to pick an appropriate (abi3) pre-built wheel. You may still install ``qh3`` manually
  by first upgrading your pip installation by running ``python -m pip install -U pip``.
- Fixed an issue where a server is yielding an invalid/malformed ``Alt-Svc`` header and urllib3-future may crash upon it.
- Fixed an issue where sending a ``str`` body using a ``bytes`` value for Content-Type would induce a crash.
  This was due to our unicode transparency policy. See https://github.com/jawah/urllib3.future/pull/142

2.8.905 (2024-08-04)
====================

- Fixed wrong upgrade attempt to QUIC when using a SOCKS proxy. Any usage of a proxy disable HTTP/3 over QUIC as per documented.
  until proper support is implemented in a next minor version.
- Backported upstream urllib3 #3434: util/ssl: make code resilient to missing hash functions.
  In certain environments such as in a FIPS enabled system, certain algorithms such as md5 may be unavailable. Due
  to the importing of such a module on a system where it is unavailable, urllib3(-future) will crash and is unusable.
  https://github.com/urllib3/urllib3/pull/3434
- Backported upstream urllib3 GHSA-34jh-p97f-mpxf: Strip Proxy-Authorization header on redirects.
  Added the ``Proxy-Authorization`` header to the list of headers to strip from requests when redirecting to a different host.
  As before, different headers can be set via ``Retry.remove_headers_on_redirect``.
- Fixed state-machine desync on a rare scenario when uploading a body using HTTP/3 over QUIC.

2.8.904 (2024-07-18)
====================

- Relaxed h11 constraint around "pending proposal" and coming server event about upgrade.
  This is made to ensure near perfect compatibility against the legacy urllib3 which is based on http.client.
- Fixed h11 yielding bytearray instead of bytes in rare circumstances.
- Added ``docker-py`` in our CI/integration pipeline.

2.8.903 (2024-07-17)
====================

- Added ``IS_PYOPENSSL`` constant that is exposed by upstream in ``urllib3.util.ssl_`` submodule.
- Fixed missing exception (``ImportError``) when importing ``urllib3.contrib.pyopenssl`` when PyOpenSSL isn't present in environment.
- Lowered and simplified testing requirements for HTTP/2, and HTTP/3.
- Added ``boto3``, ``sphinx``, and ``requests`` to our downstream test cases (nox).

2.8.902 (2024-07-07)
====================

- Added support for async iterable yielding either bytes or str when passing a body into your requests.
- Added dummy module (e.g. http2 and emscriptem) like upstream without serving any of them. Those modules won't be served and are empty as we diverged since.
- Added a better error message for http3 handshake failure to help out users figuring out what is happening.
- Added official support for Python 3.13

2.8.901 (2024-06-27)
====================

- Improved compatibility with httplib exception for ``IncompleteRead`` that did not behave exactly like expected (repr/str format over it).
- The metric TLS handshake delay was wrongfully set when using HTTP/2 over cleartext.
- Fixed compatibility with some third-party mocking library that are injecting io.BytesIO in HTTPResponse.
  In some cases, ``IncompleteRead`` might not be raised like expected.

2.8.900 (2024-06-24)
====================

- Support for HTTP/2 with prior knowledge over non-encrypted connection to leverage multiplexing in internal networks.
  To leverage this feature, you have to disable HTTP/1.1 so that `urllib3-future` can infer your intent.
  Disabling HTTP/1.1 is to be made as follow: ``PoolManager(disabled_svn={HttpVersion.h11})``.
- Added raw data bytes counter in ``LowLevelResponse`` to help end-users track download speed accordingly if they use
  brotli, gzip or zstd transfer-encoding during downloads.

2.7.914 (2024-06-15)
====================

- Further improved compatibility with some third party programs that accessed hazardous materials within http.client standard library.
- Add "ARM64" architecture for qh3 automatic installation on Windows.

2.7.913 (2024-05-31)
====================

- Relaxed constraints around ``HTTPConnectionPool._new_conn`` private method in order to ensure a broader compatibility. (#122)

2.7.912 (2024-05-27)
====================

- Fixed unset ``tls_version`` within ``ConnectionInfo`` when using the legacy TLSv1 protocol.
- Fixed license metadata SPDX in package.
- Fixed custom ssl context with ``OP_NO_TLSv1_3`` option that did not disable HTTP/3.
- Fixed custom ssl context with ``assert_hostname=False`` parameter not forwarded to QUIC configuration.

2.7.911 (2024-05-24)
====================

- Fixed the ability to override properly the ``:authority`` special header via the legacy ``Host`` header.

2.7.910 (2024-05-22)
====================

- Removed workaround for a bug that existed in qh3 < 1.0 with cryptography in a concurrent (thread) environment.
- Avoid loading qh3 at runtime in order to improve import delay. It was used to probe HTTP/3 support. We compute it lazily from now on.
- Added the possibility to use the ``preemptive_quic_cache`` MutableMapping to exclude endpoints.
  If your implementation discard the recently set key/entry it will prevent the connection from upgrading itself.

2.7.909 (2024-05-17)
====================

- Improve (large) data download performance by increasing the default blocksize.
- Improve HTTP/1.1 performance by reducing the amount of time we want to infer "if next cycle" should be triggered.

2.7.908 (2024-05-16)
====================

- Improve ``traffic_state_of`` function to improve the overall performance in a highly concurrent context.

2.7.907 (2024-05-05)
====================

- Passing a ssl context containing manually loaded root certificates no longer is ignored with HTTP/3 over QUIC.

2.7.906 (2024-05-02)
====================

- Overall performance improvement with HTTP/2 in a highly concurrent context.

2.7.905 (2024-04-28)
====================

- Added support for ``jh2>=5,<6`` instead of ``h2~=4.0`` as a drop-in replacement.
  Expect a significant performance improvement with HTTP/2. We successfully reduced our dependency footprint to the minimum.

2.7.904 (2024-04-20)
====================

- Added support for qh3 version v1
- **Security:** Fixed ignored DNS matching with its certificate in certain conditions while negotiating HTTP/3 over QUIC

2.7.903 (2024-04-04)
====================

- Removed warning about "unresponsive" pool of connection due to how it can confuse users.

2.7.902 (2024-04-03)
====================

- Fixed a rare racing condition occurring on PyPy when using DNS-over-HTTPS leading to a socket.gaierror exception.
- Fixed retrieving the dict peer certificate when ``cert_reqs=0`` aka. disabled TLS over TCP verification.

2.7.901 (2024-03-27)
====================

- Fixed an edge case with Response::read() confusing situation where passing a positive amount to read then
  passing ``None`` n-times would continuously return cached data if the stream was closed (content consumed).
- Fixed IncompleteRead exception property ``expected`` that did not contain the "remaining" amount expected but rather
  the total expected.

2.7.900 (2024-03-25)
====================

- Added Happy-Eyeballs support.
  This feature is disabled by default, you can enable it by passing ``happy_eyeballs=True``
  into ``AsyncPoolManager``, ``AsyncHTTPConnectionPool`` or its synchronous counterparts.
  See the documentation to learn more.
- Fixed an issue where passing a IPv6 address to the in-memory resolver provider would be improperly registered.
- Fixed unclosed socket when the user attempt to set a impossible port to bind on (i.e. not in range 0-65535) leading to a ResourceWarning.
- Fixed a rare issue with DNS-over-HTTPS where a HTTPS record would also be interpreted as a normal record.

2.6.906 (2024-03-18)
====================

- Fixed SSL context cache construction that did not take key_password into account.
- Prefer return ``NotImplemented`` instead of raising ``NotImplementedError`` to avoid polluting the stack trace when trying to
  initialize the external tls layer when not concerned (e.g. not http3 over QUIC).

2.6.905 (2024-03-17)
====================

- Fixed traffic police shutdown procedure to avoid killing needlessly a new connection or pool.

2.6.904 (2024-03-17)
====================

- Overall performance improvements for both async and sync calls.
- Removed ``TrafficPolice`` internal caching for obj states of contained elements due to its inability to be up-to-date in some cases.
- Fixed SSLError wrong message displayed when using the underlying ``qh3`` library (HTTP/3 only).
- Fixed graceful shutdown for rare HTTP/2 servers configured to immediately forbid opening new streams.

2.6.903 (2024-03-10)
====================

- Overall performance improvements for both async and sync calls.

2.6.902 (2024-03-04)
====================

- Fixed PyPy error when running asynchronous code on Windows after trying to create a datagram socket.
  This error is due to an incomplete implementation of the Windows socket API. We silently disabled HTTP/3
  if running PyPy+Windows+asyncio until upstream issue resolution.
- Overall performance improvements for both async and sync calls.
- Fixed ProtocolError (No recent network activity after XYZ) error when it should recycle the connection automatically (sync only).
- Added a user-friendly error message when invoking ``get_response`` from either ``PoolManager`` or ``ConnectionPool`` with anything
  else than a ``ResponsePromise``.

2.6.901 (2024-02-28)
====================

- Fixed blocking IO just after HTTP/3 is negotiated in an asynchronous context.
- Added explicit warning in case your pool of connections is insufficiently sized for the given charge in an asynchronous context.
- Fixed automatic retrieval of the issuer certificate in an asynchronous context (``ConnectionInfo``).

2.6.900 (2024-02-26)
====================

- Added full asynchronous support using asyncio.
  urllib3.future officially support asyncio as his asynchronous scheduler.
  The following public classes are immediately available:

  ``AsyncPoolManager``, ``AsyncHTTPConnectionPool``, ``AsyncHTTPSConnectionPool``, ``AsyncProxyManager``,
  ``AsyncResolverDescription``.

  Finally, bellow functions are also available:

  ``async_proxy_from_url``, and ``async_connection_from_url``.

  Explore the documentation section about async to learn more about this awesome feature with detailed
  examples. No extra dependencies are required. We rely exclusively on the standard library.

  Async SOCKS proxies are also supported at no additional costs with ``contrib.socks.AsyncSOCKSProxyManager``.

2.5.904 (2024-02-21)
====================

- Improved reliability with PoliceTraffic.borrow with type as indicator when heavily accessed by many threads.

2.5.903 (2024-02-20)
====================

- Fixed an edge case where a simultaneous call to ``get_response()`` without a specific promise could lead to a non-thread safe operation.

2.5.902 (2024-02-04)
====================

- Fixed missed cleanup of unused PoolKey stored in ``PoliceTraffic`` upon a full ``PoolManager``.

2.5.901 (2024-02-02)
====================

- Fixed a compatibility issue with ``boto3`` when trying to send data (got an unexpected keyword argument). #79

2.5.900 (2024-02-02)
====================

- Improved performance and reliability for concurrent streams handled by a single connection.
  We relied on a flat array of events generated by the protocol state-machine that unfortunately was not
  efficient. urllib3-future now handle the events with a matrix/multi-dimensional array bound to time.
- Fixed a thread safety issue when a single multiplexed connection was used across many threads.
  We revised in-depth the logic wrapper around the connection locking to make sure you may
  go all-in when using threads in that particular context. In consequence to that:
  1) We are, effective immediately, deprecating ``RecentlyUsedContainer``
  in favor of our internal ``PoliceTraffic`` that was used in ``PoolManager``.
  2) No longer using ``Queue`` to manage the ``Connection`` in ``HTTPConnectionPool``.
  If you try to set ``HTTPConnectionPool.QueueCls`` it will raise a deprecation warning.
  Starting today, we no longer accept implementation like ``queue.Queue`` because it
  cannot fit the need of this complex HTTP client, especially with the multiplexing aspect.
- Increased default pool maxsize for DNS-over-HTTPS from 1 to 10.

2.4.906 (2024-01-19)
====================

- Fixed a rare case of HTTP/3 being disabled when forwarding a custom SSLContext created.
- Re-introduce ``DEFAULT_CIPHERS`` constant in ``urllib3.util.ssl_`` due to the demands.
  It contains the Mozilla recommended cipher suite that was introduced in version 2.2.900.
- Fixed handling of OpenSSL 3.2.0 new error message for misconfiguring an HTTP proxy as HTTPS.
  Ported from urllib3/3271.
- Fixed ``request_sent_latency`` that wasn't computed when request was stopped early (prior to sending the
  complete body).

2.4.905 (2024-01-16)
====================

- Fixed an edge case where a HTTPS record was misinterpreted when using a DNS-over-HTTPS resolver.

2.4.904 (2024-01-15)
====================

- Fixed an issue where a idle QUIC connection would not be recycled properly when expired.
- Added support for passing ``-1`` as the **amt** in ``HTTPResponse`` (read, or stream) as the strict equivalent of ``read1``.
  This allows you to fetch content as soon as it arrive.
- Removed orphaned method ``_handle_chunk``, ``_update_chunk_length`` from ``HTTPResponse``.
- Fixed the iterator in ``HTTPResponse`` that hung until the complete content was downloaded.

2.4.903 (2024-01-07)
====================

- Fixed an issue where setting None for a header value could cause an exception.

2.4.902 (2024-01-01)
====================

- Fixed compatibility with older PyPy 3.7 interpreters when HTTP/3 (qh3) can be unavailable.
- Fixed undesired DGRAM/QUIC preemptive upgrade using insecure protocol.

2.4.901 (2023-12-31)
====================

- Fixed an issue where a stateless resolver (e.g. nullresolver) could not be recycled.
- Fixed an issue where one would attempt to close a resolver multiple times.

2.4.900 (2023-12-30)
====================

- Added issuer certificate extraction from SSLSocket with native calls with Python 3.10+ in ``ConnectionInfo``.
- Added support for DNS over TLS, DNS over HTTPS, DNS over QUIC, DNS over UDP, and local hosts-like DNS.
  ``PoolManager``, and ``HTTPPoolManager`` constructor now expose an additional keyword argument, ``resolver=...``.
  You can assign to it one of the presented protocol. Also, you may chain a list of resolver, each resolver can be
  limited to a list of host-pattern or not. Default is the system DNS. This new feature is covered by our thread-safety
  promise.

  You can now do the following: ``PoolManage(resolver="doh://dns.google")`` for example.
  Refer to the official documentation to learn about the full capabilities.
- Support for SOCKS proxies is now provided by `python-socks` instead of `PySocks` due to being largely
  unmaintained within a reasonable period of time. This change is made completely transparent.
- Added details in ``ConnectionInfo`` about detailed timings and others details.
  ``established_latency`` is a _timedelta_ that represent the amount of time consumed to get an ESTABLISHED network link.
  ``resolution_latency`` is a _timedelta_ that represent the amount of time consumed for the hostname resolution.
  ``tls_handshake_latency`` is a _timedelta_ that represent the amount of time consumed for the TLS handshake.
  ``request_sent_latency`` is a _timedelta_ that represent the amount of time consumed to encode and send the whole request through the socket.
- Fixed a rare thread safety issue when using at least one HTTP/3 multiplexed connection.
- Deprecated function ``util.connection.create_connection(..)`` in favor of newly added ``contrib.resolver`` that will
  host from now on that function within ``BaseResolver`` as a method. Users are encouraged to migrate as soon as possible.
- Support for preemptively negotiating HTTP/3 over QUIC based on RFC 9460 via a HTTPS DNS record.
- Added support for enforcing IPv6, and/or IPv4 using the keyword parameter ``socket_family`` that can be provided in
  ``PoolManager``, ``HTTP(S)ConnectionPool`` and ``HTTP(S)Connection``. The three accepted values are ``socket.AF_UNSPEC``
  ``socket.AF_INET``, and ``socket.AF_INET6``. Respectively, allow all, ipv4 only, and ipv6 only. Anything else will raise
  **ValueError**.

2.3.902 (2023-12-08)
====================

- Fixed an issue where specifying `cert_reqs=ssl.CERT_NONE` or `assert_hostname` was ignored when using HTTP/3 over QUIC.

2.3.901 (2023-11-26)
====================

- Small performance improvement while in HTTP/1.1
- Any string passed down to the body will enforce a default ``Content-Type: text/plain; charset=utf-8`` for safety, unless
  you specified a ``Content-Type`` header yourself. The ``charset`` parameter will always be set to ``utf-8``.
  It is recommended that you pass ``bytes`` instead of a plain string. If a conflicting charset has been set that
  does not refer to utf-8, a warning will be raised.
- Added callable argument in ``urlopen``, and ``request`` named ``on_upload_body`` that enable you to track
  body upload progress for a single request. It takes 4 positional arguments, namely:
  (total_sent: int, total_to_be_sent: int | None, is_completed: bool, any_error: bool)
  total_to_be_sent may be set to None if we're unable to know in advance the total size (blind iterator/generator).
- Fixed a rare case where ``ProtocolError`` was raised instead of expected ``IncompleteRead`` exception.
- Improved HTTP/3 overall performance.
- Changed the default max connection per host for (http, https) pools managed by ``PoolManager``.
  If the ``PoolManager`` is instantiated with ``num_pools=10``, each (managed) subsequent pool will have ``maxsize=10``.
- Improved performance while in a multithreading context while using many multiplexed connections.
- Changed the default max saturated multiplexed connections to 64 as the minimum.
  Now a warning will be fired if you reach the maximum capacity of stored saturated multiplexed connections.

2.3.900 (2023-11-18)
====================

- Disabled unsafe renegotiation option with TLS by default where applicable.
- Added fallback package ``urllib3_future`` in addition to ``urllib3``.
  This became increasingly needed as a significant number of projects requires ``urllib3`` and
  accidentally override this fork.

2.2.907 (2023-11-11)
====================

- Reverted relying on ``qh3`` to dynamically retrieve the max concurrent streams allowed before connection saturation.

2.2.906 (2023-11-11)
====================

- Bumped minimum requirement for ``qh3`` to version 0.14.0 in order to drop private calls in ``contrib.hface.protocols._qh3``.
- Cache last 1024 ``parse_url`` function call as it is costly.
- Fixed incomplete flow control window checks while sending data in HTTP/2.
- Fixed unexpected BrokenPipeError exception in a rare edge case.
- Changed behavior for efficiency around ``socket.recv`` to pull ``conn.blocksize`` bytes regardless of ``Response.read(amt=...)``.

2.2.905 (2023-11-08)
====================

- Fixed loss of a QUIC connection due to an inappropriate check in ``conn.is_connected``.
- Separate saturated (multiplexed) connections from the main pool to a distinct one.

2.2.904 (2023-11-06)
====================

- Fixed concurrent/multiplexed request overflow in a full connection pool.
- Fixed connection close that had in-flight request (in multiplexed mode), the connection appeared as not idle on clean reuse.

2.2.903 (2023-11-06)
====================

- Improved overall performances in HTTP/2, and HTTP/3, with or without multiplexed.

2.2.902 (2023-11-05)
====================

- Fixed QUIC connection not taking ``cert_data`` due to an accidental variable override.

2.2.901 (2023-11-04)
====================

- Fixed several issues with multiplexing.
  (i) Fixed max concurrent streams in HTTP/2, and HTTP/3.
  (ii) Fixed tracking of unconsumed response prior to try upgrade the connection (to HTTP/3).
  (iii) Fixed (always) releasing multiplexed connections into pool.
  (iv) Fixed request having body being interrupted by the ``EarlyResponse`` exception 'signal'.

2.2.900 (2023-11-01)
====================

- Added support for in-memory client (intermediary) certificate to be used with mTLS.
  This feature compensate for the complete removal of ``pyOpenSSL``. Unfortunately it is only
  available on Linux, OpenBSD, and FreeBSD. Using newly added ``cert_data`` and ``key_data`` arguments
  in ``HTTPSConnection`` and ``HTTPSPoolConnection`` you will be capable of passing the certificate along with
  its key without getting nowhere near your filesystem.
  MacOS and Windows are not concerned by this feature when using HTTP/1.1, and HTTP/2 with TLS over TCP.
- Removed remnant ``SSLTransport.makefile`` as it was built to circumvent a legacy constraint when urllib3 depended upon
  ``http.client``.
- Bumped minimum requirement for ``qh3`` to version 0.13.0 in order to support in-memory client certificate (mTLS).
- Symbolic complete detachment from ``http.client``. Removed all references and imports to ``http.client``. Farewell!
- Changed the default ciphers in default SSLContext for an **increased** security level.
  *Rational:* Earlier in v2.1.901 we initialized the SSLContext ciphers with the value ``DEFAULT`` but after much
  consideration, after we saw that the associated ciphers (e.g. ``DEFAULT`` from OpenSSL) includes some weak suites
  we decided to inject a rather safer and limited cipher suite. It is based on https://ssl-config.mozilla.org
  Starting now, urllib3.future will match Mozilla cipher recommendations (intermediary) and will regularly update the suite.
- Added support for multiplexed connection. HTTP/2 and HTTP/3 can benefit from this.
  urllib3.future no longer blocks when ``urlopen(...)`` is invoked using ``multiplexed=True``, and return
  a ``ResponsePromise`` instead of a ``HTTPResponse``. You may dispatch as much requests as the protocol
  permits you (concurrent stream) and then retrieve the response(s) using the ``get_response(...)``.
  ``get_response(...)`` can take up to one kwarg to specify the target promise, if none specified, will retrieve
  the first available response. ``multiplexed`` is set to False by default and will likely be the default for a long
  time.
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

  You may do multiplexing using ``PoolManager``, and ``HTTPSPoolConnection``. Connection upgrade
  to HTTP/3 cannot be done until all in-flight requests are completed.
  Be aware that a non-capable connection (e.g. HTTP/1.1) will just ignore the ``multiplexed=True`` setting
  and act traditionally.
- Connection are now released into their respective pool when the connection support multiplexing (HTTP/2, HTTP/3)
  before the response has been consumed. This allows to have multiple response half-consumed from a single connection.

2.1.903 (2023-10-23)
====================

- Removed ``BaseHTTPConnection``, and ``BaseHTTPSConnection``.
  Rationale: The initial idea, as far as I understand it, was to create a ``HTTPSConnection`` per protocols, e.g.
  HTTP/2, and HTTP/3. From the point of view of ``urllib3.future`` it was taken care of in ``contrib.hface``
  where the protocols state-machines are handled. We plan to always have a unified ``Connection`` class that
  regroup all protocols for convenience. The private module ``urllib3._base_connection`` is renamed to ``urllib3._typing``.
  It brings a lot of simplification, which is welcomed.
- Reduced ``BaseHTTPResponse`` to a mere alias of ``HTTPResponse`` for the same reasoning as before. There is absolutely
  no need whatsoever in the foreseeable future to ship urllib3.future with an alternative implementation of ``HTTPResponse``.
  It will be removed in a future major.
- Removed ``RECENT_DATE`` and linked logic as it does not make sense to (i) maintain it (ii) the certificate verification
  failure won't be avoided anyway, so it is a warning prior to an unavoidable error. The warning class ``SystemTimeWarning``
  will be removed in a future major.
- Added support for stopping sending body if the server responded early in HTTP/2, or HTTP/3.
  This can happen when a server says that you exhausted the size limit or if previously sent
  headers were rejected for example. This should save a lot of time to users in given cases.
- Refactored scattered typing aliases across the sources. ``urllib3._typing`` now contain all of our definitions.
- Avoid installation of ``qh3`` in PyPy 3.11+ while pre-built wheels are unavailable.

2.1.902 (2023-10-21)
====================

- Fixed an issue where streaming response did not yield data until the stream was closed.
- Unified peercert/issuercert dict output in ConnectionInfo output format when HTTP/3.
- Made body stripped from HTTP requests changing the request method to GET after HTTP 303 "See Other" redirect responses.
  Headers ``content-encoding, content-language, content-location, content-type, content-length, digest, last-modified`` are
  also stripped in the said case.
  Port of the security fix GHSA-g4mx-q9vg-27p4
- ``_TYPE_BODY`` now accept `Iterable[str]` in addition to `Iterable[bytes]`.

2.1.901 (2023-10-10)
====================

- Set ``DEFAULT`` (as OpenSSL default list) for ciphers in SSLContext if none is provided instead of Python default.
- Fixed an edge case where chosen state machine would be indicated to not end stream where it should.
- Fixed a rare case where ``ProtocolError`` was raised instead of ``SSLError`` in the underlying QUIC layer state-machine.
- Small performance improvement in sending a body by removing an obsolete logic made for a removed constraint.
- Changed default ``User-Agent`` to ``urllib3.future/x.y.z``.
- Removed a compatibility operation that added a ``Content-Length`` header on request with unknown body length.
  This was present due to a bug in Traefik server. A investigation will be conducted and a relevant issue will be
  addressed.

2.1.900 (2023-10-07)
====================

- Added ``cipher`` in ``ConnectionInfo`` when using HTTP/3 over QUIC.
- Added ``issuer_certificate_der``, ``issuer_certificate_dict`` into ``ConnectionInfo``.

  By default, it is set to ``None``. This property is filled automatically on a QUIC connection.
  It cannot be done automatically when using native Python capabilities.

- Removed support for SecureTransport.
- Removed support for PyOpenSSL.

  This module is not delete but rendered ineffective. An explicit warning still appear.

- Improved automated exchange between the socket and the HTTP state machines.
- Removed all dependencies in the ``secure`` extra.
- Fixed disabling HTTP/3 over QUIC if specified settings were incompatible with TLS over QUIC.

  Previously if ``ssl_context`` was set and specifying a list of ciphers it was discarded on upgrade.
  Also, if ``ssl_maximum_version`` was set to TLS v1.2.
  Now those parameters are correctly forwarded to the custom QUIC/TLS layer.

- Fixed ``ConnectionInfo`` repr that did not shown the ``http_version`` property.
- Undeprecated 'ssl_version' option in create_urllib3_context.
- Undeprecated 'format_header_param_rfc2231'.
- Removed warning about the 'strict' parameter.
- Removed constant ``IS_PYOPENSSL`` and ``IS_SECURETRANSPORT`` from ``urllib3.utils``.
- Added raise warning when using environment variables ``SSLKEYLOGFILE``, and ``QUICLOGDIR``.
- Added the ``Cookie`` header to the list of headers to strip from requests when redirecting to a different host. As before, different headers can be set via ``Retry.remove_headers_on_redirect``.
- Removed warning about ssl not being the ``OpenSSL`` backend. You are free to choose.

  Users are simply encouraged to report issues if any to the jawah/urllib3.future repository.
  Support will be provided by the best of our abilities.

2.0.936 (2023-10-01)
====================

- Added support for event ``StreamReset`` to raise a ``ProtocolError`` when received from either h2 or h3. (`#28 <https://github.com/jawah/urllib3.future/issues/28>`__)


2.0.935 (2023-10-01)
====================

- Fixed a violation in our QUIC transmission due to sending multiple datagram at once. (`#26 <https://github.com/jawah/urllib3.future/issues/26>`__)


2.0.934 (2023-09-23)
====================

- Added public `ConnectionInfo` class that will be present in each `HttpConnection` instance.

  Passing the kwarg ``on_post_connection`` that accept a callable with a single positional argument
  in ``PoolManager.urlopen`` method will result in a call each time a connection is picked out
  of the pool. The function will be passed a ``ConnectionInfo`` object.
  The same argument (``on_post_connection``) can be passed down to the ``HTTPConnectionPool.urlopen`` method. (`#23 <https://github.com/jawah/urllib3.future/issues/23>`__)

- `#22 <https://github.com/jawah/urllib3.future/issues/22>`__


2.0.933 (2023-09-21)
====================

- Fixed ``HTTPSConnectionPool`` not accepting and forwarding ``ca_cert_data``. (`#20 <https://github.com/jawah/urllib3.future/issues/20>`__)


2.0.932 (2023-09-12)
====================

- Fixed `assert_hostname` behavior when HTTPSConnection targets HTTP/3 over QUIC (`#8 <https://github.com/jawah/urllib3.future/issues/8>`__)
- Fixed protocol violation for HTTP/2 and HTTP/3 where we sent ``Connection: keep-alive`` when it is
  forbidden. (`#16 <https://github.com/jawah/urllib3.future/issues/16>`__)
- Fixed ``unpack_chunk`` workaround function in the ``send`` method when body is multipart/form-data (`#17 <https://github.com/jawah/urllib3.future/issues/17>`__)
- Fixed the flow control when sending a body for a HTTP/2 connection.
  The body will be split into numerous chunks if the size exceed the specified blocksize when not
  using HTTP/1.1 in order to avoid ProtocolError (flow control) (`#18 <https://github.com/jawah/urllib3.future/issues/18>`__)


2.0.931 (2023-07-16)
====================

Features
--------

- Added experimental support for HTTP/1.1, HTTP/2 and HTTP/3 independently of httplib.

  Currently urllib3 does not offer async http request and the backend is the http.client package
  shipped alongside Python. This implementation is not scheduled to improve, even less to support latest
  protocol.

  Without proxies, the negotiation is as follow:

  - http requests are always made using HTTP/1.1.
  - https requests are made with HTTP/2 if TLS-ALPN yield its support otherwise HTTP/1.1.

  - https requests may upgrade to HTTP/3 if latest response contain a valid Alt-Svc header.

  With proxies:

  - The initial proxy request is always issued using HTTP/1.1 regardless if its http or https.
  - Subsequents requests follow the previous section (Without proxies) at the sole exception that HTTP/3 upgrade is disabled.

  You may explicitly disable HTTP/2 or, and, HTTP/3 by passing ``disabled_svn={HttpVersion.h2}`` to your ``BaseHttpConnection`` instance.
  Disabling HTTP/1.1 is forbidden and raise an error.

  Note that a valid or accepted Alt-Svc header in urllib3 means looking for the "h3" (final specification) protocol and disallow switching hostname for security
  reasons. (`#1 <https://github.com/jawah/urllib3.future/issues/1>`__)
- Added ``BaseHTTPResponse`` to ``__all__`` in ``__init__.py`` (`#3078 <https://github.com/urllib3/urllib3/issues/3078>`__)


2.0.3 (2023-06-07)
==================

- Allowed alternative SSL libraries such as LibreSSL, while still issuing a warning as we cannot help users facing issues with implementations other than OpenSSL. (`#3020 <https://github.com/urllib3/urllib3/issues/3020>`__)
- Deprecated URLs which don't have an explicit scheme (`#2950 <https://github.com/urllib3/urllib3/pull/2950>`_)
- Fixed response decoding with Zstandard when compressed data is made of several frames. (`#3008 <https://github.com/urllib3/urllib3/issues/3008>`__)
- Fixed ``assert_hostname=False`` to correctly skip hostname check. (`#3051 <https://github.com/urllib3/urllib3/issues/3051>`__)


2.0.2 (2023-05-03)
==================

- Fixed ``HTTPResponse.stream()`` to continue yielding bytes if buffered decompressed data
  was still available to be read even if the underlying socket is closed. This prevents
  a compressed response from being truncated. (`#3009 <https://github.com/urllib3/urllib3/issues/3009>`__)


2.0.1 (2023-04-30)
==================

- Fixed a socket leak when fingerprint or hostname verifications fail. (`#2991 <https://github.com/urllib3/urllib3/issues/2991>`__)
- Fixed an error when ``HTTPResponse.read(0)`` was the first ``read`` call or when the internal response body buffer was otherwise empty. (`#2998 <https://github.com/urllib3/urllib3/issues/2998>`__)


2.0.0 (2023-04-26)
==================

Read the `v2.0 migration guide <https://urllib3.readthedocs.io/en/latest/v2-migration-guide.html>`__ for help upgrading to the latest version of urllib3.

Removed
-------

* Removed support for Python 2.7, 3.5, and 3.6 (`#883 <https://github.com/urllib3/urllib3/issues/883>`__, `#2336 <https://github.com/urllib3/urllib3/issues/2336>`__).
* Removed fallback on certificate ``commonName`` in ``match_hostname()`` function.
  This behavior was deprecated in May 2000 in RFC 2818. Instead only ``subjectAltName``
  is used to verify the hostname by default. To enable verifying the hostname against
  ``commonName`` use ``SSLContext.hostname_checks_common_name = True`` (`#2113 <https://github.com/urllib3/urllib3/issues/2113>`__).
* Removed support for Python with an ``ssl`` module compiled with LibreSSL, CiscoSSL,
  wolfSSL, and all other OpenSSL alternatives. Python is moving to require OpenSSL with PEP 644 (`#2168 <https://github.com/urllib3/urllib3/issues/2168>`__).
* Removed support for OpenSSL versions earlier than 1.1.1 or that don't have SNI support.
  When an incompatible OpenSSL version is detected an ``ImportError`` is raised (`#2168 <https://github.com/urllib3/urllib3/issues/2168>`__).
* Removed the list of default ciphers for OpenSSL 1.1.1+ and SecureTransport as their own defaults are already secure (`#2082 <https://github.com/urllib3/urllib3/issues/2082>`__).
* Removed ``urllib3.contrib.appengine.AppEngineManager`` and support for Google App Engine Standard Environment (`#2044 <https://github.com/urllib3/urllib3/issues/2044>`__).
* Removed deprecated ``Retry`` options ``method_whitelist``, ``DEFAULT_REDIRECT_HEADERS_BLACKLIST`` (`#2086 <https://github.com/urllib3/urllib3/issues/2086>`__).
* Removed ``urllib3.HTTPResponse.from_httplib`` (`#2648 <https://github.com/urllib3/urllib3/issues/2648>`__).
* Removed default value of ``None`` for the ``request_context`` parameter of ``urllib3.PoolManager.connection_from_pool_key``. This change should have no effect on users as the default value of ``None`` was an invalid option and was never used (`#1897 <https://github.com/urllib3/urllib3/issues/1897>`__).
* Removed the ``urllib3.request`` module. ``urllib3.request.RequestMethods`` has been made a private API.
  This change was made to ensure that ``from urllib3 import request`` imported the top-level ``request()``
  function instead of the ``urllib3.request`` module (`#2269 <https://github.com/urllib3/urllib3/issues/2269>`__).
* Removed support for SSLv3.0 from the ``urllib3.contrib.pyopenssl`` even when support is available from the compiled OpenSSL library (`#2233 <https://github.com/urllib3/urllib3/issues/2233>`__).
* Removed the deprecated ``urllib3.contrib.ntlmpool`` module (`#2339 <https://github.com/urllib3/urllib3/issues/2339>`__).
* Removed ``DEFAULT_CIPHERS``, ``HAS_SNI``, ``USE_DEFAULT_SSLCONTEXT_CIPHERS``, from the private module ``urllib3.util.ssl_`` (`#2168 <https://github.com/urllib3/urllib3/issues/2168>`__).
* Removed ``urllib3.exceptions.SNIMissingWarning`` (`#2168 <https://github.com/urllib3/urllib3/issues/2168>`__).
* Removed the ``_prepare_conn`` method from ``HTTPConnectionPool``. Previously this was only used to call ``HTTPSConnection.set_cert()`` by ``HTTPSConnectionPool`` (`#1985 <https://github.com/urllib3/urllib3/issues/1985>`__).
* Removed ``tls_in_tls_required`` property from ``HTTPSConnection``. This is now determined from the ``scheme`` parameter in ``HTTPConnection.set_tunnel()`` (`#1985 <https://github.com/urllib3/urllib3/issues/1985>`__).
* Removed the ``strict`` parameter/attribute from ``HTTPConnection``, ``HTTPSConnection``, ``HTTPConnectionPool``, ``HTTPSConnectionPool``, and ``HTTPResponse`` (`#2064 <https://github.com/urllib3/urllib3/issues/2064>`__).

Deprecated
----------

* Deprecated ``HTTPResponse.getheaders()`` and ``HTTPResponse.getheader()`` which will be removed in urllib3 v2.1.0. Instead use ``HTTPResponse.headers`` and ``HTTPResponse.headers.get(name, default)``. (`#1543 <https://github.com/urllib3/urllib3/issues/1543>`__, `#2814 <https://github.com/urllib3/urllib3/issues/2814>`__).
* Deprecated ``urllib3.contrib.pyopenssl`` module which will be removed in urllib3 v2.1.0 (`#2691 <https://github.com/urllib3/urllib3/issues/2691>`__).
* Deprecated ``urllib3.contrib.securetransport`` module which will be removed in urllib3 v2.1.0 (`#2692 <https://github.com/urllib3/urllib3/issues/2692>`__).
* Deprecated ``ssl_version`` option in favor of ``ssl_minimum_version``. ``ssl_version`` will be removed in urllib3 v2.1.0 (`#2110 <https://github.com/urllib3/urllib3/issues/2110>`__).
* Deprecated the ``strict`` parameter of ``PoolManager.connection_from_context()`` as it's not longer needed in Python 3.x. It will be removed in urllib3 v2.1.0 (`#2267 <https://github.com/urllib3/urllib3/issues/2267>`__)
* Deprecated the ``NewConnectionError.pool`` attribute which will be removed in urllib3 v2.1.0 (`#2271 <https://github.com/urllib3/urllib3/issues/2271>`__).
* Deprecated ``format_header_param_html5`` and ``format_header_param`` in favor of ``format_multipart_header_param`` (`#2257 <https://github.com/urllib3/urllib3/issues/2257>`__).
* Deprecated ``RequestField.header_formatter`` parameter which will be removed in urllib3 v2.1.0 (`#2257 <https://github.com/urllib3/urllib3/issues/2257>`__).
* Deprecated ``HTTPSConnection.set_cert()`` method. Instead pass parameters to the ``HTTPSConnection`` constructor (`#1985 <https://github.com/urllib3/urllib3/issues/1985>`__).
* Deprecated ``HTTPConnection.request_chunked()`` method which will be removed in urllib3 v2.1.0. Instead pass ``chunked=True`` to ``HTTPConnection.request()`` (`#1985 <https://github.com/urllib3/urllib3/issues/1985>`__).

Added
-----

* Added top-level ``urllib3.request`` function which uses a preconfigured module-global ``PoolManager`` instance (`#2150 <https://github.com/urllib3/urllib3/issues/2150>`__).
* Added the ``json`` parameter to ``urllib3.request()``, ``PoolManager.request()``, and ``ConnectionPool.request()`` methods to send JSON bodies in requests. Using this parameter will set the header ``Content-Type: application/json`` if ``Content-Type`` isn't already defined.
  Added support for parsing JSON response bodies with ``HTTPResponse.json()`` method (`#2243 <https://github.com/urllib3/urllib3/issues/2243>`__).
* Added type hints to the ``urllib3`` module (`#1897 <https://github.com/urllib3/urllib3/issues/1897>`__).
* Added ``ssl_minimum_version`` and ``ssl_maximum_version`` options which set
  ``SSLContext.minimum_version`` and ``SSLContext.maximum_version`` (`#2110 <https://github.com/urllib3/urllib3/issues/2110>`__).
* Added support for Zstandard (RFC 8878) when ``zstandard`` 1.18.0 or later is installed.
  Added the ``zstd`` extra which installs the ``zstandard`` package (`#1992 <https://github.com/urllib3/urllib3/issues/1992>`__).
* Added ``urllib3.response.BaseHTTPResponse`` class. All future response classes will be subclasses of ``BaseHTTPResponse`` (`#2083 <https://github.com/urllib3/urllib3/issues/2083>`__).
* Added ``FullPoolError`` which is raised when ``PoolManager(block=True)`` and a connection is returned to a full pool (`#2197 <https://github.com/urllib3/urllib3/issues/2197>`__).
* Added ``HTTPHeaderDict`` to the top-level ``urllib3`` namespace (`#2216 <https://github.com/urllib3/urllib3/issues/2216>`__).
* Added support for configuring header merging behavior with HTTPHeaderDict
  When using a ``HTTPHeaderDict`` to provide headers for a request, by default duplicate
  header values will be repeated. But if ``combine=True`` is passed into a call to
  ``HTTPHeaderDict.add``, then the added header value will be merged in with an existing
  value into a comma-separated list (``X-My-Header: foo, bar``) (`#2242 <https://github.com/urllib3/urllib3/issues/2242>`__).
* Added ``NameResolutionError`` exception when a DNS error occurs (`#2305 <https://github.com/urllib3/urllib3/issues/2305>`__).
* Added ``proxy_assert_hostname`` and ``proxy_assert_fingerprint`` kwargs to ``ProxyManager`` (`#2409 <https://github.com/urllib3/urllib3/issues/2409>`__).
* Added a configurable ``backoff_max`` parameter to the ``Retry`` class.
  If a custom ``backoff_max`` is provided to the ``Retry`` class, it
  will replace the ``Retry.DEFAULT_BACKOFF_MAX`` (`#2494 <https://github.com/urllib3/urllib3/issues/2494>`__).
* Added the ``authority`` property to the Url class as per RFC 3986 3.2. This property should be used in place of ``netloc`` for users who want to include the userinfo (auth) component of the URI (`#2520 <https://github.com/urllib3/urllib3/issues/2520>`__).
* Added the ``scheme`` parameter to ``HTTPConnection.set_tunnel`` to configure the scheme of the origin being tunnelled to (`#1985 <https://github.com/urllib3/urllib3/issues/1985>`__).
* Added the ``is_closed``, ``is_connected`` and ``has_connected_to_proxy`` properties to ``HTTPConnection`` (`#1985 <https://github.com/urllib3/urllib3/issues/1985>`__).
* Added optional ``backoff_jitter`` parameter to ``Retry``. (`#2952 <https://github.com/urllib3/urllib3/issues/2952>`__)

Changed
-------

* Changed ``urllib3.response.HTTPResponse.read`` to respect the semantics of ``io.BufferedIOBase`` regardless of compression. Specifically, this method:

  * Only returns an empty bytes object to indicate EOF (that is, the response has been fully consumed).
  * Never returns more bytes than requested.
  * Can issue any number of system calls: zero, one or multiple.

  If you want each ``urllib3.response.HTTPResponse.read`` call to issue a single system call, you need to disable decompression by setting ``decode_content=False`` (`#2128 <https://github.com/urllib3/urllib3/issues/2128>`__).
* Changed ``urllib3.HTTPConnection.getresponse`` to return an instance of ``urllib3.HTTPResponse`` instead of ``http.client.HTTPResponse`` (`#2648 <https://github.com/urllib3/urllib3/issues/2648>`__).
* Changed ``ssl_version`` to instead set the corresponding ``SSLContext.minimum_version``
  and ``SSLContext.maximum_version`` values.  Regardless of ``ssl_version`` passed
  ``SSLContext`` objects are now constructed using ``ssl.PROTOCOL_TLS_CLIENT`` (`#2110 <https://github.com/urllib3/urllib3/issues/2110>`__).
* Changed default ``SSLContext.minimum_version`` to be ``TLSVersion.TLSv1_2`` in line with Python 3.10 (`#2373 <https://github.com/urllib3/urllib3/issues/2373>`__).
* Changed ``ProxyError`` to wrap any connection error (timeout, TLS, DNS) that occurs when connecting to the proxy (`#2482 <https://github.com/urllib3/urllib3/pull/2482>`__).
* Changed ``urllib3.util.create_urllib3_context`` to not override the system cipher suites with
  a default value. The new default will be cipher suites configured by the operating system (`#2168 <https://github.com/urllib3/urllib3/issues/2168>`__).
* Changed ``multipart/form-data`` header parameter formatting matches the WHATWG HTML Standard as of 2021-06-10. Control characters in filenames are no longer percent encoded (`#2257 <https://github.com/urllib3/urllib3/issues/2257>`__).
* Changed the error raised when connecting via HTTPS when the ``ssl`` module isn't available from ``SSLError`` to ``ImportError`` (`#2589 <https://github.com/urllib3/urllib3/issues/2589>`__).
* Changed ``HTTPConnection.request()`` to always use lowercase chunk boundaries when sending requests with ``Transfer-Encoding: chunked`` (`#2515 <https://github.com/urllib3/urllib3/issues/2515>`__).
* Changed ``enforce_content_length`` default to True, preventing silent data loss when reading streamed responses (`#2514 <https://github.com/urllib3/urllib3/issues/2514>`__).
* Changed internal implementation of ``HTTPHeaderDict`` to use ``dict`` instead of ``collections.OrderedDict`` for better performance (`#2080 <https://github.com/urllib3/urllib3/issues/2080>`__).
* Changed the ``urllib3.contrib.pyopenssl`` module to wrap ``OpenSSL.SSL.Error`` with ``ssl.SSLError`` in ``PyOpenSSLContext.load_cert_chain`` (`#2628 <https://github.com/urllib3/urllib3/issues/2628>`__).
* Changed usage of the deprecated ``socket.error`` to ``OSError`` (`#2120 <https://github.com/urllib3/urllib3/issues/2120>`__).
* Changed all parameters in the ``HTTPConnection`` and ``HTTPSConnection`` constructors to be keyword-only except ``host`` and ``port`` (`#1985 <https://github.com/urllib3/urllib3/issues/1985>`__).
* Changed ``HTTPConnection.getresponse()`` to set the socket timeout from ``HTTPConnection.timeout`` value before reading
  data from the socket. This previously was done manually by the ``HTTPConnectionPool`` calling ``HTTPConnection.sock.settimeout(...)`` (`#1985 <https://github.com/urllib3/urllib3/issues/1985>`__).
* Changed the ``_proxy_host`` property to ``_tunnel_host`` in ``HTTPConnectionPool`` to more closely match how the property is used (value in ``HTTPConnection.set_tunnel()``) (`#1985 <https://github.com/urllib3/urllib3/issues/1985>`__).
* Changed name of ``Retry.BACK0FF_MAX`` to be ``Retry.DEFAULT_BACKOFF_MAX``.
* Changed TLS handshakes to use ``SSLContext.check_hostname`` when possible (`#2452 <https://github.com/urllib3/urllib3/pull/2452>`__).
* Changed ``server_hostname`` to behave like other parameters only used by ``HTTPSConnectionPool`` (`#2537 <https://github.com/urllib3/urllib3/pull/2537>`__).
* Changed the default ``blocksize`` to 16KB to match OpenSSL's default read amounts (`#2348 <https://github.com/urllib3/urllib3/pull/2348>`__).
* Changed ``HTTPResponse.read()`` to raise an error when calling with ``decode_content=False`` after using ``decode_content=True`` to prevent data loss (`#2800 <https://github.com/urllib3/urllib3/issues/2800>`__).

Fixed
-----

* Fixed thread-safety issue where accessing a ``PoolManager`` with many distinct origins would cause connection pools to be closed while requests are in progress (`#1252 <https://github.com/urllib3/urllib3/issues/1252>`__).
* Fixed an issue where an ``HTTPConnection`` instance would erroneously reuse the socket read timeout value from reading the previous response instead of a newly configured connect timeout.
  Instead now if ``HTTPConnection.timeout`` is updated before sending the next request the new timeout value will be used (`#2645 <https://github.com/urllib3/urllib3/issues/2645>`__).
* Fixed ``socket.error.errno`` when raised from pyOpenSSL's ``OpenSSL.SSL.SysCallError`` (`#2118 <https://github.com/urllib3/urllib3/issues/2118>`__).
* Fixed the default value of ``HTTPSConnection.socket_options`` to match ``HTTPConnection`` (`#2213 <https://github.com/urllib3/urllib3/issues/2213>`__).
* Fixed a bug where ``headers`` would be modified by the ``remove_headers_on_redirect`` feature (`#2272 <https://github.com/urllib3/urllib3/issues/2272>`__).
* Fixed a reference cycle bug in ``urllib3.util.connection.create_connection()`` (`#2277 <https://github.com/urllib3/urllib3/issues/2277>`__).
* Fixed a socket leak if ``HTTPConnection.connect()`` fails (`#2571 <https://github.com/urllib3/urllib3/pull/2571>`__).
* Fixed ``urllib3.contrib.pyopenssl.WrappedSocket`` and ``urllib3.contrib.securetransport.WrappedSocket`` close methods (`#2970 <https://github.com/urllib3/urllib3/issues/2970>`__)

1.26.16 (2023-05-23)
====================

* Fixed thread-safety issue where accessing a ``PoolManager`` with many distinct origins
  would cause connection pools to be closed while requests are in progress (`#2954 <https://github.com/urllib3/urllib3/pull/2954>`_)

1.26.15 (2023-03-10)
====================

* Fix socket timeout value when ``HTTPConnection`` is reused (`#2645 <https://github.com/urllib3/urllib3/issues/2645>`__)
* Remove "!" character from the unreserved characters in IPv6 Zone ID parsing
  (`#2899 <https://github.com/urllib3/urllib3/issues/2899>`__)
* Fix IDNA handling of '\x80' byte (`#2901 <https://github.com/urllib3/urllib3/issues/2901>`__)

1.26.14 (2023-01-11)
====================

* Fixed parsing of port 0 (zero) returning None, instead of 0. (`#2850 <https://github.com/urllib3/urllib3/issues/2850>`__)
* Removed deprecated getheaders() calls in contrib module. Fixed the type hint of ``PoolKey.key_retries`` by adding ``bool`` to the union. (`#2865 <https://github.com/urllib3/urllib3/issues/2865>`__)

1.26.13 (2022-11-23)
====================

* Deprecated the ``HTTPResponse.getheaders()`` and ``HTTPResponse.getheader()`` methods.
* Fixed an issue where parsing a URL with leading zeroes in the port would be rejected
  even when the port number after removing the zeroes was valid.
* Fixed a deprecation warning when using cryptography v39.0.0.
* Removed the ``<4`` in the ``Requires-Python`` packaging metadata field.

1.26.12 (2022-08-22)
====================

* Deprecated the `urllib3[secure]` extra and the `urllib3.contrib.pyopenssl` module.
  Both will be removed in v2.x. See this `GitHub issue <https://github.com/urllib3/urllib3/issues/2680>`_
  for justification and info on how to migrate.

1.26.11 (2022-07-25)
====================

* Fixed an issue where reading more than 2 GiB in a call to ``HTTPResponse.read`` would
  raise an ``OverflowError`` on Python 3.9 and earlier.

1.26.10 (2022-07-07)
====================

* Removed support for Python 3.5
* Fixed an issue where a ``ProxyError`` recommending configuring the proxy as HTTP
  instead of HTTPS could appear even when an HTTPS proxy wasn't configured.

1.26.9 (2022-03-16)
===================

* Changed ``urllib3[brotli]`` extra to favor installing Brotli libraries that are still
  receiving updates like ``brotli`` and ``brotlicffi`` instead of ``brotlipy``.
  This change does not impact behavior of urllib3, only which dependencies are installed.
* Fixed a socket leaking when ``HTTPSConnection.connect()`` raises an exception.
* Fixed ``server_hostname`` being forwarded from ``PoolManager`` to ``HTTPConnectionPool``
  when requesting an HTTP URL. Should only be forwarded when requesting an HTTPS URL.

1.26.8 (2022-01-07)
===================

* Added extra message to ``urllib3.exceptions.ProxyError`` when urllib3 detects that
  a proxy is configured to use HTTPS but the proxy itself appears to only use HTTP.
* Added a mention of the size of the connection pool when discarding a connection due to the pool being full.
* Added explicit support for Python 3.11.
* Deprecated the ``Retry.MAX_BACKOFF`` class property in favor of ``Retry.DEFAULT_MAX_BACKOFF``
  to better match the rest of the default parameter names. ``Retry.MAX_BACKOFF`` is removed in v2.0.
* Changed location of the vendored ``ssl.match_hostname`` function from ``urllib3.packages.ssl_match_hostname``
  to ``urllib3.util.ssl_match_hostname`` to ensure Python 3.10+ compatibility after being repackaged
  by downstream distributors.
* Fixed absolute imports, all imports are now relative.


1.26.7 (2021-09-22)
===================

* Fixed a bug with HTTPS hostname verification involving IP addresses and lack
  of SNI. (Issue #2400)
* Fixed a bug where IPv6 braces weren't stripped during certificate hostname
  matching. (Issue #2240)


1.26.6 (2021-06-25)
===================

* Deprecated the ``urllib3.contrib.ntlmpool`` module. urllib3 is not able to support
  it properly due to `reasons listed in this issue <https://github.com/urllib3/urllib3/issues/2282>`_.
  If you are a user of this module please leave a comment.
* Changed ``HTTPConnection.request_chunked()`` to not erroneously emit multiple
  ``Transfer-Encoding`` headers in the case that one is already specified.
* Fixed typo in deprecation message to recommend ``Retry.DEFAULT_ALLOWED_METHODS``.


1.26.5 (2021-05-26)
===================

* Fixed deprecation warnings emitted in Python 3.10.
* Updated vendored ``six`` library to 1.16.0.
* Improved performance of URL parser when splitting
  the authority component.


1.26.4 (2021-03-15)
===================

* Changed behavior of the default ``SSLContext`` when connecting to HTTPS proxy
  during HTTPS requests. The default ``SSLContext`` now sets ``check_hostname=True``.


1.26.3 (2021-01-26)
===================

* Fixed bytes and string comparison issue with headers (Pull #2141)

* Changed ``ProxySchemeUnknown`` error message to be
  more actionable if the user supplies a proxy URL without
  a scheme. (Pull #2107)


1.26.2 (2020-11-12)
===================

* Fixed an issue where ``wrap_socket`` and ``CERT_REQUIRED`` wouldn't
  be imported properly on Python 2.7.8 and earlier (Pull #2052)


1.26.1 (2020-11-11)
===================

* Fixed an issue where two ``User-Agent`` headers would be sent if a
  ``User-Agent`` header key is passed as ``bytes`` (Pull #2047)


1.26.0 (2020-11-10)
===================

* **NOTE: urllib3 v2.0 will drop support for Python 2**.
  `Read more in the v2.0 Roadmap <https://urllib3.readthedocs.io/en/latest/v2-roadmap.html>`_.

* Added support for HTTPS proxies contacting HTTPS servers (Pull #1923, Pull #1806)

* Deprecated negotiating TLSv1 and TLSv1.1 by default. Users that
  still wish to use TLS earlier than 1.2 without a deprecation warning
  should opt-in explicitly by setting ``ssl_version=ssl.PROTOCOL_TLSv1_1`` (Pull #2002)
  **Starting in urllib3 v2.0: Connections that receive a ``DeprecationWarning`` will fail**

* Deprecated ``Retry`` options ``Retry.DEFAULT_METHOD_WHITELIST``, ``Retry.DEFAULT_REDIRECT_HEADERS_BLACKLIST``
  and ``Retry(method_whitelist=...)`` in favor of ``Retry.DEFAULT_ALLOWED_METHODS``,
  ``Retry.DEFAULT_REMOVE_HEADERS_ON_REDIRECT``, and ``Retry(allowed_methods=...)``
  (Pull #2000) **Starting in urllib3 v2.0: Deprecated options will be removed**

* Added default ``User-Agent`` header to every request (Pull #1750)

* Added ``urllib3.util.SKIP_HEADER`` for skipping ``User-Agent``, ``Accept-Encoding``,
  and ``Host`` headers from being automatically emitted with requests (Pull #2018)

* Collapse ``transfer-encoding: chunked`` request data and framing into
  the same ``socket.send()`` call (Pull #1906)

* Send ``http/1.1`` ALPN identifier with every TLS handshake by default (Pull #1894)

* Properly terminate SecureTransport connections when CA verification fails (Pull #1977)

* Don't emit an ``SNIMissingWarning`` when passing ``server_hostname=None``
  to SecureTransport (Pull #1903)

* Disabled requesting TLSv1.2 session tickets as they weren't being used by urllib3 (Pull #1970)

* Suppress ``BrokenPipeError`` when writing request body after the server
  has closed the socket (Pull #1524)

* Wrap ``ssl.SSLError`` that can be raised from reading a socket (e.g. "bad MAC")
  into an ``urllib3.exceptions.SSLError`` (Pull #1939)


1.25.11 (2020-10-19)
====================

* Fix retry backoff time parsed from ``Retry-After`` header when given
  in the HTTP date format. The HTTP date was parsed as the local timezone
  rather than accounting for the timezone in the HTTP date (typically
  UTC) (Pull #1932, Pull #1935, Pull #1938, Pull #1949)

* Fix issue where an error would be raised when the ``SSLKEYLOGFILE``
  environment variable was set to the empty string. Now ``SSLContext.keylog_file``
  is not set in this situation (Pull #2016)


1.25.10 (2020-07-22)
====================

* Added support for ``SSLKEYLOGFILE`` environment variable for
  logging TLS session keys with use with programs like
  Wireshark for decrypting captured web traffic (Pull #1867)

* Fixed loading of SecureTransport libraries on macOS Big Sur
  due to the new dynamic linker cache (Pull #1905)

* Collapse chunked request bodies data and framing into one
  call to ``send()`` to reduce the number of TCP packets by 2-4x (Pull #1906)

* Don't insert ``None`` into ``ConnectionPool`` if the pool
  was empty when requesting a connection (Pull #1866)

* Avoid ``hasattr`` call in ``BrotliDecoder.decompress()`` (Pull #1858)


1.25.9 (2020-04-16)
===================

* Added ``InvalidProxyConfigurationWarning`` which is raised when
  erroneously specifying an HTTPS proxy URL. urllib3 doesn't currently
  support connecting to HTTPS proxies but will soon be able to
  and we would like users to migrate properly without much breakage.

  See `this GitHub issue <https://github.com/urllib3/urllib3/issues/1850>`_
  for more information on how to fix your proxy config. (Pull #1851)

* Drain connection after ``PoolManager`` redirect (Pull #1817)

* Ensure ``load_verify_locations`` raises ``SSLError`` for all backends (Pull #1812)

* Rename ``VerifiedHTTPSConnection`` to ``HTTPSConnection`` (Pull #1805)

* Allow the CA certificate data to be passed as a string (Pull #1804)

* Raise ``ValueError`` if method contains control characters (Pull #1800)

* Add ``__repr__`` to ``Timeout`` (Pull #1795)


1.25.8 (2020-01-20)
===================

* Drop support for EOL Python 3.4 (Pull #1774)

* Optimize _encode_invalid_chars (Pull #1787)


1.25.7 (2019-11-11)
===================

* Preserve ``chunked`` parameter on retries (Pull #1715, Pull #1734)

* Allow unset ``SERVER_SOFTWARE`` in App Engine (Pull #1704, Issue #1470)

* Fix issue where URL fragment was sent within the request target. (Pull #1732)

* Fix issue where an empty query section in a URL would fail to parse. (Pull #1732)

* Remove TLS 1.3 support in SecureTransport due to Apple removing support (Pull #1703)


1.25.6 (2019-09-24)
===================

* Fix issue where tilde (``~``) characters were incorrectly
  percent-encoded in the path. (Pull #1692)


1.25.5 (2019-09-19)
===================

* Add mitigation for BPO-37428 affecting Python <3.7.4 and OpenSSL 1.1.1+ which
  caused certificate verification to be enabled when using ``cert_reqs=CERT_NONE``.
  (Issue #1682)


1.25.4 (2019-09-19)
===================

* Propagate Retry-After header settings to subsequent retries. (Pull #1607)

* Fix edge case where Retry-After header was still respected even when
  explicitly opted out of. (Pull #1607)

* Remove dependency on ``rfc3986`` for URL parsing.

* Fix issue where URLs containing invalid characters within ``Url.auth`` would
  raise an exception instead of percent-encoding those characters.

* Add support for ``HTTPResponse.auto_close = False`` which makes HTTP responses
  work well with BufferedReaders and other ``io`` module features. (Pull #1652)

* Percent-encode invalid characters in URL for ``HTTPConnectionPool.request()`` (Pull #1673)


1.25.3 (2019-05-23)
===================

* Change ``HTTPSConnection`` to load system CA certificates
  when ``ca_certs``, ``ca_cert_dir``, and ``ssl_context`` are
  unspecified. (Pull #1608, Issue #1603)

* Upgrade bundled rfc3986 to v1.3.2. (Pull #1609, Issue #1605)


1.25.2 (2019-04-28)
===================

* Change ``is_ipaddress`` to not detect IPvFuture addresses. (Pull #1583)

* Change ``parse_url`` to percent-encode invalid characters within the
  path, query, and target components. (Pull #1586)


1.25.1 (2019-04-24)
===================

* Add support for Google's ``Brotli`` package. (Pull #1572, Pull #1579)

* Upgrade bundled rfc3986 to v1.3.1 (Pull #1578)


1.25 (2019-04-22)
=================

* Require and validate certificates by default when using HTTPS (Pull #1507)

* Upgraded ``urllib3.utils.parse_url()`` to be RFC 3986 compliant. (Pull #1487)

* Added support for ``key_password`` for ``HTTPSConnectionPool`` to use
  encrypted ``key_file`` without creating your own ``SSLContext`` object. (Pull #1489)

* Add TLSv1.3 support to CPython, pyOpenSSL, and SecureTransport ``SSLContext``
  implementations. (Pull #1496)

* Switched the default multipart header encoder from RFC 2231 to HTML 5 working draft. (Issue #303, Pull #1492)

* Fixed issue where OpenSSL would block if an encrypted client private key was
  given and no password was given. Instead an ``SSLError`` is raised. (Pull #1489)

* Added support for Brotli content encoding. It is enabled automatically if
  ``brotlipy`` package is installed which can be requested with
  ``urllib3[brotli]`` extra. (Pull #1532)

* Drop ciphers using DSS key exchange from default TLS cipher suites.
  Improve default ciphers when using SecureTransport. (Pull #1496)

* Implemented a more efficient ``HTTPResponse.__iter__()`` method. (Issue #1483)

1.24.3 (2019-05-01)
===================

* Apply fix for CVE-2019-9740. (Pull #1591)

1.24.2 (2019-04-17)
===================

* Don't load system certificates by default when any other ``ca_certs``, ``ca_certs_dir`` or
  ``ssl_context`` parameters are specified.

* Remove Authorization header regardless of case when redirecting to cross-site. (Issue #1510)

* Add support for IPv6 addresses in subjectAltName section of certificates. (Issue #1269)


1.24.1 (2018-11-02)
===================

* Remove quadratic behavior within ``GzipDecoder.decompress()`` (Issue #1467)

* Restored functionality of ``ciphers`` parameter for ``create_urllib3_context()``. (Issue #1462)


1.24 (2018-10-16)
=================

* Allow key_server_hostname to be specified when initializing a PoolManager to allow custom SNI to be overridden. (Pull #1449)

* Test against Python 3.7 on AppVeyor. (Pull #1453)

* Early-out ipv6 checks when running on App Engine. (Pull #1450)

* Change ambiguous description of backoff_factor (Pull #1436)

* Add ability to handle multiple Content-Encodings (Issue #1441 and Pull #1442)

* Skip DNS names that can't be idna-decoded when using pyOpenSSL (Issue #1405).

* Add a server_hostname parameter to HTTPSConnection which allows for
  overriding the SNI hostname sent in the handshake. (Pull #1397)

* Drop support for EOL Python 2.6 (Pull #1429 and Pull #1430)

* Fixed bug where responses with header Content-Type: message/* erroneously
  raised HeaderParsingError, resulting in a warning being logged. (Pull #1439)

* Move urllib3 to src/urllib3 (Pull #1409)


1.23 (2018-06-04)
=================

* Allow providing a list of headers to strip from requests when redirecting
  to a different host. Defaults to the ``Authorization`` header. Different
  headers can be set via ``Retry.remove_headers_on_redirect``. (Issue #1316)

* Fix ``util.selectors._fileobj_to_fd`` to accept ``long`` (Issue #1247).

* Dropped Python 3.3 support. (Pull #1242)

* Put the connection back in the pool when calling stream() or read_chunked() on
  a chunked HEAD response. (Issue #1234)

* Fixed pyOpenSSL-specific ssl client authentication issue when clients
  attempted to auth via certificate + chain (Issue #1060)

* Add the port to the connectionpool connect print (Pull #1251)

* Don't use the ``uuid`` module to create multipart data boundaries. (Pull #1380)

* ``read_chunked()`` on a closed response returns no chunks. (Issue #1088)

* Add Python 2.6 support to ``contrib.securetransport`` (Pull #1359)

* Added support for auth info in url for SOCKS proxy (Pull #1363)


1.22 (2017-07-20)
=================

* Fixed missing brackets in ``HTTP CONNECT`` when connecting to IPv6 address via
  IPv6 proxy. (Issue #1222)

* Made the connection pool retry on ``SSLError``.  The original ``SSLError``
  is available on ``MaxRetryError.reason``. (Issue #1112)

* Drain and release connection before recursing on retry/redirect.  Fixes
  deadlocks with a blocking connectionpool. (Issue #1167)

* Fixed compatibility for cookiejar. (Issue #1229)

* pyopenssl: Use vendored version of ``six``. (Issue #1231)


1.21.1 (2017-05-02)
===================

* Fixed SecureTransport issue that would cause long delays in response body
  delivery. (Pull #1154)

* Fixed regression in 1.21 that threw exceptions when users passed the
  ``socket_options`` flag to the ``PoolManager``.  (Issue #1165)

* Fixed regression in 1.21 that threw exceptions when users passed the
  ``assert_hostname`` or ``assert_fingerprint`` flag to the ``PoolManager``.
  (Pull #1157)


1.21 (2017-04-25)
=================

* Improved performance of certain selector system calls on Python 3.5 and
  later. (Pull #1095)

* Resolved issue where the PyOpenSSL backend would not wrap SysCallError
  exceptions appropriately when sending data. (Pull #1125)

* Selectors now detects a monkey-patched select module after import for modules
  that patch the select module like eventlet, greenlet. (Pull #1128)

* Reduced memory consumption when streaming zlib-compressed responses
  (as opposed to raw deflate streams). (Pull #1129)

* Connection pools now use the entire request context when constructing the
  pool key. (Pull #1016)

* ``PoolManager.connection_from_*`` methods now accept a new keyword argument,
  ``pool_kwargs``, which are merged with the existing ``connection_pool_kw``.
  (Pull #1016)

* Add retry counter for ``status_forcelist``. (Issue #1147)

* Added ``contrib`` module for using SecureTransport on macOS:
  ``urllib3.contrib.securetransport``.  (Pull #1122)

* urllib3 now only normalizes the case of ``http://`` and ``https://`` schemes:
  for schemes it does not recognise, it assumes they are case-sensitive and
  leaves them unchanged.
  (Issue #1080)


1.20 (2017-01-19)
=================

* Added support for waiting for I/O using selectors other than select,
  improving urllib3's behaviour with large numbers of concurrent connections.
  (Pull #1001)

* Updated the date for the system clock check. (Issue #1005)

* ConnectionPools now correctly consider hostnames to be case-insensitive.
  (Issue #1032)

* Outdated versions of PyOpenSSL now cause the PyOpenSSL contrib module
  to fail when it is injected, rather than at first use. (Pull #1063)

* Outdated versions of cryptography now cause the PyOpenSSL contrib module
  to fail when it is injected, rather than at first use. (Issue #1044)

* Automatically attempt to rewind a file-like body object when a request is
  retried or redirected. (Pull #1039)

* Fix some bugs that occur when modules incautiously patch the queue module.
  (Pull #1061)

* Prevent retries from occurring on read timeouts for which the request method
  was not in the method whitelist. (Issue #1059)

* Changed the PyOpenSSL contrib module to lazily load idna to avoid
  unnecessarily bloating the memory of programs that don't need it. (Pull
  #1076)

* Add support for IPv6 literals with zone identifiers. (Pull #1013)

* Added support for socks5h:// and socks4a:// schemes when working with SOCKS
  proxies, and controlled remote DNS appropriately. (Issue #1035)


1.19.1 (2016-11-16)
===================

* Fixed AppEngine import that didn't function on Python 3.5. (Pull #1025)


1.19 (2016-11-03)
=================

* urllib3 now respects Retry-After headers on 413, 429, and 503 responses when
  using the default retry logic. (Pull #955)

* Remove markers from setup.py to assist ancient setuptools versions. (Issue
  #986)

* Disallow superscripts and other integerish things in URL ports. (Issue #989)

* Allow urllib3's HTTPResponse.stream() method to continue to work with
  non-httplib underlying FPs. (Pull #990)

* Empty filenames in multipart headers are now emitted as such, rather than
  being suppressed. (Issue #1015)

* Prefer user-supplied Host headers on chunked uploads. (Issue #1009)


1.18.1 (2016-10-27)
===================

* CVE-2016-9015. Users who are using urllib3 version 1.17 or 1.18 along with
  PyOpenSSL injection and OpenSSL 1.1.0 *must* upgrade to this version. This
  release fixes a vulnerability whereby urllib3 in the above configuration
  would silently fail to validate TLS certificates due to erroneously setting
  invalid flags in OpenSSL's ``SSL_CTX_set_verify`` function. These erroneous
  flags do not cause a problem in OpenSSL versions before 1.1.0, which
  interprets the presence of any flag as requesting certificate validation.

  There is no PR for this patch, as it was prepared for simultaneous disclosure
  and release. The master branch received the same fix in Pull #1010.


1.18 (2016-09-26)
=================

* Fixed incorrect message for IncompleteRead exception. (Pull #973)

* Accept ``iPAddress`` subject alternative name fields in TLS certificates.
  (Issue #258)

* Fixed consistency of ``HTTPResponse.closed`` between Python 2 and 3.
  (Issue #977)

* Fixed handling of wildcard certificates when using PyOpenSSL. (Issue #979)


1.17 (2016-09-06)
=================

* Accept ``SSLContext`` objects for use in SSL/TLS negotiation. (Issue #835)

* ConnectionPool debug log now includes scheme, host, and port. (Issue #897)

* Substantially refactored documentation. (Issue #887)

* Used URLFetch default timeout on AppEngine, rather than hardcoding our own.
  (Issue #858)

* Normalize the scheme and host in the URL parser (Issue #833)

* ``HTTPResponse`` contains the last ``Retry`` object, which now also
  contains retries history. (Issue #848)

* Timeout can no longer be set as boolean, and must be greater than zero.
  (Pull #924)

* Removed pyasn1 and ndg-httpsclient from dependencies used for PyOpenSSL. We
  now use cryptography and idna, both of which are already dependencies of
  PyOpenSSL. (Pull #930)

* Fixed infinite loop in ``stream`` when amt=None. (Issue #928)

* Try to use the operating system's certificates when we are using an
  ``SSLContext``. (Pull #941)

* Updated cipher suite list to allow ChaCha20+Poly1305. AES-GCM is preferred to
  ChaCha20, but ChaCha20 is then preferred to everything else. (Pull #947)

* Updated cipher suite list to remove 3DES-based cipher suites. (Pull #958)

* Removed the cipher suite fallback to allow HIGH ciphers. (Pull #958)

* Implemented ``length_remaining`` to determine remaining content
  to be read. (Pull #949)

* Implemented ``enforce_content_length`` to enable exceptions when
  incomplete data chunks are received. (Pull #949)

* Dropped connection start, dropped connection reset, redirect, forced retry,
  and new HTTPS connection log levels to DEBUG, from INFO. (Pull #967)


1.16 (2016-06-11)
=================

* Disable IPv6 DNS when IPv6 connections are not possible. (Issue #840)

* Provide ``key_fn_by_scheme`` pool keying mechanism that can be
  overridden. (Issue #830)

* Normalize scheme and host to lowercase for pool keys, and include
  ``source_address``. (Issue #830)

* Cleaner exception chain in Python 3 for ``_make_request``.
  (Issue #861)

* Fixed installing ``urllib3[socks]`` extra. (Issue #864)

* Fixed signature of ``ConnectionPool.close`` so it can actually safely be
  called by subclasses. (Issue #873)

* Retain ``release_conn`` state across retries. (Issues #651, #866)

* Add customizable ``HTTPConnectionPool.ResponseCls``, which defaults to
  ``HTTPResponse`` but can be replaced with a subclass. (Issue #879)


1.15.1 (2016-04-11)
===================

* Fix packaging to include backports module. (Issue #841)


1.15 (2016-04-06)
=================

* Added Retry(raise_on_status=False). (Issue #720)

* Always use setuptools, no more distutils fallback. (Issue #785)

* Dropped support for Python 3.2. (Issue #786)

* Chunked transfer encoding when requesting with ``chunked=True``.
  (Issue #790)

* Fixed regression with IPv6 port parsing. (Issue #801)

* Append SNIMissingWarning messages to allow users to specify it in
  the PYTHONWARNINGS environment variable. (Issue #816)

* Handle unicode headers in Py2. (Issue #818)

* Log certificate when there is a hostname mismatch. (Issue #820)

* Preserve order of request/response headers. (Issue #821)


1.14 (2015-12-29)
=================

* contrib: SOCKS proxy support! (Issue #762)

* Fixed AppEngine handling of transfer-encoding header and bug
  in Timeout defaults checking. (Issue #763)


1.13.1 (2015-12-18)
===================

* Fixed regression in IPv6 + SSL for match_hostname. (Issue #761)


1.13 (2015-12-14)
=================

* Fixed ``pip install urllib3[secure]`` on modern pip. (Issue #706)

* pyopenssl: Fixed SSL3_WRITE_PENDING error. (Issue #717)

* pyopenssl: Support for TLSv1.1 and TLSv1.2. (Issue #696)

* Close connections more defensively on exception. (Issue #734)

* Adjusted ``read_chunked`` to handle gzipped, chunk-encoded bodies without
  repeatedly flushing the decoder, to function better on Jython. (Issue #743)

* Accept ``ca_cert_dir`` for SSL-related PoolManager configuration. (Issue #758)


1.12 (2015-09-03)
=================

* Rely on ``six`` for importing ``httplib`` to work around
  conflicts with other Python 3 shims. (Issue #688)

* Add support for directories of certificate authorities, as supported by
  OpenSSL. (Issue #701)

* New exception: ``NewConnectionError``, raised when we fail to establish
  a new connection, usually ``ECONNREFUSED`` socket error.


1.11 (2015-07-21)
=================

* When ``ca_certs`` is given, ``cert_reqs`` defaults to
  ``'CERT_REQUIRED'``. (Issue #650)

* ``pip install urllib3[secure]`` will install Certifi and
  PyOpenSSL as dependencies. (Issue #678)

* Made ``HTTPHeaderDict`` usable as a ``headers`` input value
  (Issues #632, #679)

* Added `urllib3.contrib.appengine <https://urllib3.readthedocs.io/en/latest/contrib.html#google-app-engine>`_
  which has an ``AppEngineManager`` for using ``URLFetch`` in a
  Google AppEngine environment. (Issue #664)

* Dev: Added test suite for AppEngine. (Issue #631)

* Fix performance regression when using PyOpenSSL. (Issue #626)

* Passing incorrect scheme (e.g. ``foo://``) will raise
  ``ValueError`` instead of ``AssertionError`` (backwards
  compatible for now, but please migrate). (Issue #640)

* Fix pools not getting replenished when an error occurs during a
  request using ``release_conn=False``. (Issue #644)

* Fix pool-default headers not applying for url-encoded requests
  like GET. (Issue #657)

* log.warning in Python 3 when headers are skipped due to parsing
  errors. (Issue #642)

* Close and discard connections if an error occurs during read.
  (Issue #660)

* Fix host parsing for IPv6 proxies. (Issue #668)

* Separate warning type SubjectAltNameWarning, now issued once
  per host. (Issue #671)

* Fix ``httplib.IncompleteRead`` not getting converted to
  ``ProtocolError`` when using ``HTTPResponse.stream()``
  (Issue #674)

1.10.4 (2015-05-03)
===================

* Migrate tests to Tornado 4. (Issue #594)

* Append default warning configuration rather than overwrite.
  (Issue #603)

* Fix streaming decoding regression. (Issue #595)

* Fix chunked requests losing state across keep-alive connections.
  (Issue #599)

* Fix hanging when chunked HEAD response has no body. (Issue #605)


1.10.3 (2015-04-21)
===================

* Emit ``InsecurePlatformWarning`` when SSLContext object is missing.
  (Issue #558)

* Fix regression of duplicate header keys being discarded.
  (Issue #563)

* ``Response.stream()`` returns a generator for chunked responses.
  (Issue #560)

* Set upper-bound timeout when waiting for a socket in PyOpenSSL.
  (Issue #585)

* Work on platforms without `ssl` module for plain HTTP requests.
  (Issue #587)

* Stop relying on the stdlib's default cipher list. (Issue #588)


1.10.2 (2015-02-25)
===================

* Fix file descriptor leakage on retries. (Issue #548)

* Removed RC4 from default cipher list. (Issue #551)

* Header performance improvements. (Issue #544)

* Fix PoolManager not obeying redirect retry settings. (Issue #553)


1.10.1 (2015-02-10)
===================

* Pools can be used as context managers. (Issue #545)

* Don't re-use connections which experienced an SSLError. (Issue #529)

* Don't fail when gzip decoding an empty stream. (Issue #535)

* Add sha256 support for fingerprint verification. (Issue #540)

* Fixed handling of header values containing commas. (Issue #533)


1.10 (2014-12-14)
=================

* Disabled SSLv3. (Issue #473)

* Add ``Url.url`` property to return the composed url string. (Issue #394)

* Fixed PyOpenSSL + gevent ``WantWriteError``. (Issue #412)

* ``MaxRetryError.reason`` will always be an exception, not string.
  (Issue #481)

* Fixed SSL-related timeouts not being detected as timeouts. (Issue #492)

* Py3: Use ``ssl.create_default_context()`` when available. (Issue #473)

* Emit ``InsecureRequestWarning`` for *every* insecure HTTPS request.
  (Issue #496)

* Emit ``SecurityWarning`` when certificate has no ``subjectAltName``.
  (Issue #499)

* Close and discard sockets which experienced SSL-related errors.
  (Issue #501)

* Handle ``body`` param in ``.request(...)``. (Issue #513)

* Respect timeout with HTTPS proxy. (Issue #505)

* PyOpenSSL: Handle ZeroReturnError exception. (Issue #520)


1.9.1 (2014-09-13)
==================

* Apply socket arguments before binding. (Issue #427)

* More careful checks if fp-like object is closed. (Issue #435)

* Fixed packaging issues of some development-related files not
  getting included. (Issue #440)

* Allow performing *only* fingerprint verification. (Issue #444)

* Emit ``SecurityWarning`` if system clock is waaay off. (Issue #445)

* Fixed PyOpenSSL compatibility with PyPy. (Issue #450)

* Fixed ``BrokenPipeError`` and ``ConnectionError`` handling in Py3.
  (Issue #443)



1.9 (2014-07-04)
================

* Shuffled around development-related files. If you're maintaining a distro
  package of urllib3, you may need to tweak things. (Issue #415)

* Unverified HTTPS requests will trigger a warning on the first request. See
  our new `security documentation
  <https://urllib3.readthedocs.io/en/latest/security.html>`_ for details.
  (Issue #426)

* New retry logic and ``urllib3.util.retry.Retry`` configuration object.
  (Issue #326)

* All raised exceptions should now wrapped in a
  ``urllib3.exceptions.HTTPException``-extending exception. (Issue #326)

* All errors during a retry-enabled request should be wrapped in
  ``urllib3.exceptions.MaxRetryError``, including timeout-related exceptions
  which were previously exempt. Underlying error is accessible from the
  ``.reason`` property. (Issue #326)

* ``urllib3.exceptions.ConnectionError`` renamed to
  ``urllib3.exceptions.ProtocolError``. (Issue #326)

* Errors during response read (such as IncompleteRead) are now wrapped in
  ``urllib3.exceptions.ProtocolError``. (Issue #418)

* Requesting an empty host will raise ``urllib3.exceptions.LocationValueError``.
  (Issue #417)

* Catch read timeouts over SSL connections as
  ``urllib3.exceptions.ReadTimeoutError``. (Issue #419)

* Apply socket arguments before connecting. (Issue #427)


1.8.3 (2014-06-23)
==================

* Fix TLS verification when using a proxy in Python 3.4.1. (Issue #385)

* Add ``disable_cache`` option to ``urllib3.util.make_headers``. (Issue #393)

* Wrap ``socket.timeout`` exception with
  ``urllib3.exceptions.ReadTimeoutError``. (Issue #399)

* Fixed proxy-related bug where connections were being reused incorrectly.
  (Issues #366, #369)

* Added ``socket_options`` keyword parameter which allows to define
  ``setsockopt`` configuration of new sockets. (Issue #397)

* Removed ``HTTPConnection.tcp_nodelay`` in favor of
  ``HTTPConnection.default_socket_options``. (Issue #397)

* Fixed ``TypeError`` bug in Python 2.6.4. (Issue #411)


1.8.2 (2014-04-17)
==================

* Fix ``urllib3.util`` not being included in the package.


1.8.1 (2014-04-17)
==================

* Fix AppEngine bug of HTTPS requests going out as HTTP. (Issue #356)

* Don't install ``dummyserver`` into ``site-packages`` as it's only needed
  for the test suite. (Issue #362)

* Added support for specifying ``source_address``. (Issue #352)


1.8 (2014-03-04)
================

* Improved url parsing in ``urllib3.util.parse_url`` (properly parse '@' in
  username, and blank ports like 'hostname:').

* New ``urllib3.connection`` module which contains all the HTTPConnection
  objects.

* Several ``urllib3.util.Timeout``-related fixes. Also changed constructor
  signature to a more sensible order. [Backwards incompatible]
  (Issues #252, #262, #263)

* Use ``backports.ssl_match_hostname`` if it's installed. (Issue #274)

* Added ``.tell()`` method to ``urllib3.response.HTTPResponse`` which
  returns the number of bytes read so far. (Issue #277)

* Support for platforms without threading. (Issue #289)

* Expand default-port comparison in ``HTTPConnectionPool.is_same_host``
  to allow a pool with no specified port to be considered equal to to an
  HTTP/HTTPS url with port 80/443 explicitly provided. (Issue #305)

* Improved default SSL/TLS settings to avoid vulnerabilities.
  (Issue #309)

* Fixed ``urllib3.poolmanager.ProxyManager`` not retrying on connect errors.
  (Issue #310)

* Disable Nagle's Algorithm on the socket for non-proxies. A subset of requests
  will send the entire HTTP request ~200 milliseconds faster; however, some of
  the resulting TCP packets will be smaller. (Issue #254)

* Increased maximum number of SubjectAltNames in ``urllib3.contrib.pyopenssl``
  from the default 64 to 1024 in a single certificate. (Issue #318)

* Headers are now passed and stored as a custom
  ``urllib3.collections_.HTTPHeaderDict`` object rather than a plain ``dict``.
  (Issue #329, #333)

* Headers no longer lose their case on Python 3. (Issue #236)

* ``urllib3.contrib.pyopenssl`` now uses the operating system's default CA
  certificates on inject. (Issue #332)

* Requests with ``retries=False`` will immediately raise any exceptions without
  wrapping them in ``MaxRetryError``. (Issue #348)

* Fixed open socket leak with SSL-related failures. (Issue #344, #348)


1.7.1 (2013-09-25)
==================

* Added granular timeout support with new ``urllib3.util.Timeout`` class.
  (Issue #231)

* Fixed Python 3.4 support. (Issue #238)


1.7 (2013-08-14)
================

* More exceptions are now pickle-able, with tests. (Issue #174)

* Fixed redirecting with relative URLs in Location header. (Issue #178)

* Support for relative urls in ``Location: ...`` header. (Issue #179)

* ``urllib3.response.HTTPResponse`` now inherits from ``io.IOBase`` for bonus
  file-like functionality. (Issue #187)

* Passing ``assert_hostname=False`` when creating a HTTPSConnectionPool will
  skip hostname verification for SSL connections. (Issue #194)

* New method ``urllib3.response.HTTPResponse.stream(...)`` which acts as a
  generator wrapped around ``.read(...)``. (Issue #198)

* IPv6 url parsing enforces brackets around the hostname. (Issue #199)

* Fixed thread race condition in
  ``urllib3.poolmanager.PoolManager.connection_from_host(...)`` (Issue #204)

* ``ProxyManager`` requests now include non-default port in ``Host: ...``
  header. (Issue #217)

* Added HTTPS proxy support in ``ProxyManager``. (Issue #170 #139)

* New ``RequestField`` object can be passed to the ``fields=...`` param which
  can specify headers. (Issue #220)

* Raise ``urllib3.exceptions.ProxyError`` when connecting to proxy fails.
  (Issue #221)

* Use international headers when posting file names. (Issue #119)

* Improved IPv6 support. (Issue #203)


1.6 (2013-04-25)
================

* Contrib: Optional SNI support for Py2 using PyOpenSSL. (Issue #156)

* ``ProxyManager`` automatically adds ``Host: ...`` header if not given.

* Improved SSL-related code. ``cert_req`` now optionally takes a string like
  "REQUIRED" or "NONE". Same with ``ssl_version`` takes strings like "SSLv23"
  The string values reflect the suffix of the respective constant variable.
  (Issue #130)

* Vendored ``socksipy`` now based on Anorov's fork which handles unexpectedly
  closed proxy connections and larger read buffers. (Issue #135)

* Ensure the connection is closed if no data is received, fixes connection leak
  on some platforms. (Issue #133)

* Added SNI support for SSL/TLS connections on Py32+. (Issue #89)

* Tests fixed to be compatible with Py26 again. (Issue #125)

* Added ability to choose SSL version by passing an ``ssl.PROTOCOL_*`` constant
  to the ``ssl_version`` parameter of ``HTTPSConnectionPool``. (Issue #109)

* Allow an explicit content type to be specified when encoding file fields.
  (Issue #126)

* Exceptions are now pickleable, with tests. (Issue #101)

* Fixed default headers not getting passed in some cases. (Issue #99)

* Treat "content-encoding" header value as case-insensitive, per RFC 2616
  Section 3.5. (Issue #110)

* "Connection Refused" SocketErrors will get retried rather than raised.
  (Issue #92)

* Updated vendored ``six``, no longer overrides the global ``six`` module
  namespace. (Issue #113)

* ``urllib3.exceptions.MaxRetryError`` contains a ``reason`` property holding
  the exception that prompted the final retry. If ``reason is None`` then it
  was due to a redirect. (Issue #92, #114)

* Fixed ``PoolManager.urlopen()`` from not redirecting more than once.
  (Issue #149)

* Don't assume ``Content-Type: text/plain`` for multi-part encoding parameters
  that are not files. (Issue #111)

* Pass `strict` param down to ``httplib.HTTPConnection``. (Issue #122)

* Added mechanism to verify SSL certificates by fingerprint (md5, sha1) or
  against an arbitrary hostname (when connecting by IP or for misconfigured
  servers). (Issue #140)

* Streaming decompression support. (Issue #159)


1.5 (2012-08-02)
================

* Added ``urllib3.add_stderr_logger()`` for quickly enabling STDERR debug
  logging in urllib3.

* Native full URL parsing (including auth, path, query, fragment) available in
  ``urllib3.util.parse_url(url)``.

* Built-in redirect will switch method to 'GET' if status code is 303.
  (Issue #11)

* ``urllib3.PoolManager`` strips the scheme and host before sending the request
  uri. (Issue #8)

* New ``urllib3.exceptions.DecodeError`` exception for when automatic decoding,
  based on the Content-Type header, fails.

* Fixed bug with pool depletion and leaking connections (Issue #76). Added
  explicit connection closing on pool eviction. Added
  ``urllib3.PoolManager.clear()``.

* 99% -> 100% unit test coverage.


1.4 (2012-06-16)
================

* Minor AppEngine-related fixes.

* Switched from ``mimetools.choose_boundary`` to ``uuid.uuid4()``.

* Improved url parsing. (Issue #73)

* IPv6 url support. (Issue #72)


1.3 (2012-03-25)
================

* Removed pre-1.0 deprecated API.

* Refactored helpers into a ``urllib3.util`` submodule.

* Fixed multipart encoding to support list-of-tuples for keys with multiple
  values. (Issue #48)

* Fixed multiple Set-Cookie headers in response not getting merged properly in
  Python 3. (Issue #53)

* AppEngine support with Py27. (Issue #61)

* Minor ``encode_multipart_formdata`` fixes related to Python 3 strings vs
  bytes.


1.2.2 (2012-02-06)
==================

* Fixed packaging bug of not shipping ``test-requirements.txt``. (Issue #47)


1.2.1 (2012-02-05)
==================

* Fixed another bug related to when ``ssl`` module is not available. (Issue #41)

* Location parsing errors now raise ``urllib3.exceptions.LocationParseError``
  which inherits from ``ValueError``.


1.2 (2012-01-29)
================

* Added Python 3 support (tested on 3.2.2)

* Dropped Python 2.5 support (tested on 2.6.7, 2.7.2)

* Use ``select.poll`` instead of ``select.select`` for platforms that support
  it.

* Use ``Queue.LifoQueue`` instead of ``Queue.Queue`` for more aggressive
  connection reusing. Configurable by overriding ``ConnectionPool.QueueCls``.

* Fixed ``ImportError`` during install when ``ssl`` module is not available.
  (Issue #41)

* Fixed ``PoolManager`` redirects between schemes (such as HTTP -> HTTPS) not
  completing properly. (Issue #28, uncovered by Issue #10 in v1.1)

* Ported ``dummyserver`` to use ``tornado`` instead of ``webob`` +
  ``eventlet``. Removed extraneous unsupported dummyserver testing backends.
  Added socket-level tests.

* More tests. Achievement Unlocked: 99% Coverage.


1.1 (2012-01-07)
================

* Refactored ``dummyserver`` to its own root namespace module (used for
  testing).

* Added hostname verification for ``VerifiedHTTPSConnection`` by vendoring in
  Py32's ``ssl_match_hostname``. (Issue #25)

* Fixed cross-host HTTP redirects when using ``PoolManager``. (Issue #10)

* Fixed ``decode_content`` being ignored when set through ``urlopen``. (Issue
  #27)

* Fixed timeout-related bugs. (Issues #17, #23)


1.0.2 (2011-11-04)
==================

* Fixed typo in ``VerifiedHTTPSConnection`` which would only present as a bug if
  you're using the object manually. (Thanks pyos)

* Made RecentlyUsedContainer (and consequently PoolManager) more thread-safe by
  wrapping the access log in a mutex. (Thanks @christer)

* Made RecentlyUsedContainer more dict-like (corrected ``__delitem__`` and
  ``__getitem__`` behaviour), with tests. Shouldn't affect core urllib3 code.


1.0.1 (2011-10-10)
==================

* Fixed a bug where the same connection would get returned into the pool twice,
  causing extraneous "HttpConnectionPool is full" log warnings.


1.0 (2011-10-08)
================

* Added ``PoolManager`` with LRU expiration of connections (tested and
  documented).
* Added ``ProxyManager`` (needs tests, docs, and confirmation that it works
  with HTTPS proxies).
* Added optional partial-read support for responses when
  ``preload_content=False``. You can now make requests and just read the headers
  without loading the content.
* Made response decoding optional (default on, same as before).
* Added optional explicit boundary string for ``encode_multipart_formdata``.
* Convenience request methods are now inherited from ``RequestMethods``. Old
  helpers like ``get_url`` and ``post_url`` should be abandoned in favour of
  the new ``request(method, url, ...)``.
* Refactored code to be even more decoupled, reusable, and extendable.
* License header added to ``.py`` files.
* Embiggened the documentation: Lots of Sphinx-friendly docstrings in the code
  and docs in ``docs/`` and on https://urllib3.readthedocs.io/.
* Embettered all the things!
* Started writing this file.


0.4.1 (2011-07-17)
==================

* Minor bug fixes, code cleanup.


0.4 (2011-03-01)
================

* Better unicode support.
* Added ``VerifiedHTTPSConnection``.
* Added ``NTLMConnectionPool`` in contrib.
* Minor improvements.


0.3.1 (2010-07-13)
==================

* Added ``assert_host_name`` optional parameter. Now compatible with proxies.


0.3 (2009-12-10)
================

* Added HTTPS support.
* Minor bug fixes.
* Refactored, broken backwards compatibility with 0.2.
* API to be treated as stable from this version forward.


0.2 (2008-11-17)
================

* Added unit tests.
* Bug fixes.


0.1 (2008-11-16)
================

* First release.
