HTTP extensions
===============

An accepted extension is available as ``response.extension``. Use
:ref:`websockets`, :ref:`server-sent-events`, and :ref:`async-websocket` for
application examples. Close the response when you finish using its extension.
An extension instance represents one active response at a time.

.. list-table:: Built-in implementations
   :header-rows: 1
   :widths: 25 30 20 25

   * - URL scheme
     - Implementation
     - HTTP versions
     - Dependency
   * - ``ws://`` / ``wss://``
     - Prefer wsproto; otherwise fast
     - HTTP/1.1
     - ``[ws]`` or ``[ws-fast]``
   * - ``ws+wsproto://`` / ``wss+wsproto://``
     - wsproto
     - HTTP/1.1
     - ``[ws]``
   * - ``ws+fast://`` / ``wss+fast://``
     - websockets Sans-I/O
     - HTTP/1.1
     - ``[ws-fast]``; Python 3.9+
   * - ``ws+rfc8441://`` / ``wss+rfc8441://``
     - wsproto extended CONNECT
     - HTTP/1.1, HTTP/2, HTTP/3
     - ``[ws]``; qh3 for HTTP/3
   * - ``psse://`` / ``sse://``
     - Native SSE reader
     - HTTP/1.1, HTTP/2, HTTP/3
     - No extra; qh3 for HTTP/3
   * - HTTP(S) with a custom upgrade or explicit raw extension
     - Raw bytes
     - Depends on the server's upgrade/CONNECT support
     - No extra; qh3 for HTTP/3

HTTP/1.1 WebSocket Upgrade succeeds with ``101 Switching Protocols``.
HTTP/2 and HTTP/3 WebSockets and custom tunnels normally return ``200 OK``
for successful CONNECT; they do not use 101. Ordinary HTTP/1.1 CONNECT tunnels
also normally return 200. SSE uses an ordinary 200 streaming response.
A successful CONNECT status is in the 2xx range. A rejected request can return
an ordinary response without an extension. When using ``multiplexed=True``, collect a returned
``ResponsePromise`` before inspecting the extension.

WebSocket methods send or return complete text/bytes messages; protocol frames
may be fragmented on the wire. ``next_payload()`` returns ``None`` at EOF.
``ping()`` sends a control frame, not an application message. Raw extensions
return byte chunks rather than messages. SSE is receive-only.

Async extension classes use awaited ``start()``, ``next_payload()``,
``send_payload()``, ``ping()`` (where provided), and ``close()``.
``closed`` reports whether the extension has ended. Their
configuration methods and properties, including ``headers()`` and
``supported_svn()``, remain synchronous. The pools normally call ``start()``;
applications should not start an already-active extension again. Built-in
extensions do not implement ``on_payload()`` callbacks; use a receive loop
around ``next_payload()``.

Server-sent event values
------------------------

.. autoclass:: urllib3.contrib.webextensions.sse.ServerSentEvent
   :exclude-members: on_payload
   :undoc-members:
   :members: event, data, id, retry, json

``event`` defaults to ``"message"`` and ``data`` is text. Multiple ``data:``
fields are joined with newlines. ``id`` carries the most recent valid event
ID; an empty ID resets it. ``retry`` is the integer supplied in that block,
in milliseconds, or ``None`` when absent. It does not schedule a reconnect.
``json()`` parses the text as JSON and propagates decoding errors.

The reader also exposes metadata-only blocks with empty data and retains
tolerant integer parsing for ``retry`` values. Comment-only blocks are skipped.
An unterminated final event is discarded at EOF. ``next_payload(raw=True)``
returns event text with its original line endings instead of this object.
See :ref:`sse-resumption` for application-managed reconnection.

Synchronous extensions
----------------------

.. autoclass:: urllib3.contrib.webextensions.sse.ServerSideEventExtensionFromHTTP
   :exclude-members: on_payload
   :undoc-members:
   :members: start, next_payload, close, closed

.. autoclass:: urllib3.contrib.webextensions.ws.WebSocketExtensionFromHTTP
   :exclude-members: on_payload
   :undoc-members:
   :members: start, next_payload, send_payload, ping, close, closed

.. autoclass:: urllib3.contrib.webextensions.ws.WebSocketExtensionFromMultiplexedHTTP
   :exclude-members: on_payload
   :undoc-members:
   :members: supported_svn, implementation

.. autoclass:: urllib3.contrib.webextensions.ws_fast.FastWebSocketExtensionFromHTTP
   :exclude-members: on_payload
   :undoc-members:
   :members: start, next_payload, send_payload, ping, close, closed

.. autoclass:: urllib3.contrib.webextensions.raw.RawExtensionFromHTTP
   :exclude-members: on_payload
   :undoc-members:
   :members: next_payload, send_payload, close, closed

Asynchronous extensions
-----------------------

.. autoclass:: urllib3.contrib.webextensions._async.sse.AsyncServerSideEventExtensionFromHTTP
   :exclude-members: on_payload
   :undoc-members:
   :members: start, next_payload, close, closed

.. autoclass:: urllib3.contrib.webextensions._async.ws.AsyncWebSocketExtensionFromHTTP
   :exclude-members: on_payload
   :undoc-members:
   :members: start, next_payload, send_payload, ping, close, closed

.. autoclass:: urllib3.contrib.webextensions._async.ws.AsyncWebSocketExtensionFromMultiplexedHTTP
   :exclude-members: on_payload
   :undoc-members:
   :members: supported_svn, implementation

.. autoclass:: urllib3.contrib.webextensions._async.ws_fast.AsyncFastWebSocketExtensionFromHTTP
   :exclude-members: on_payload
   :undoc-members:
   :members: start, next_payload, send_payload, ping, close, closed

.. autoclass:: urllib3.contrib.webextensions._async.raw.AsyncRawExtensionFromHTTP
   :exclude-members: on_payload
   :undoc-members:
   :members: next_payload, send_payload, close, closed

Extension interfaces
--------------------

For a custom extension, implement the scheme/version metadata, opening headers,
and payload/cleanup operations. Pass the instance with ``extension=`` and its
``urlopen_kwargs`` when using an ordinary HTTP(S) URL. Keep separate sync and
async implementations for blocking and awaited I/O.

.. autoclass:: urllib3.contrib.webextensions.protocol.ExtensionFromHTTP
   :exclude-members: on_payload
   :undoc-members:
   :members: supported_svn, supported_schemes, implementation, scheme_to_http_scheme, headers, urlopen_kwargs, start, next_payload, send_payload, close, closed

.. autoclass:: urllib3.contrib.webextensions._async.protocol.AsyncExtensionFromHTTP
   :exclude-members: on_payload
   :undoc-members:
   :members: supported_svn, supported_schemes, implementation, scheme_to_http_scheme, headers, urlopen_kwargs, start, next_payload, send_payload, close, closed
