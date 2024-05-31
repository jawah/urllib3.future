from __future__ import annotations

import errno
import logging
import queue
import socket
import sys
import threading
import typing
import warnings
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from socket import timeout as SocketTimeout
from types import TracebackType

from ._collections import HTTPHeaderDict
from ._request_methods import RequestMethods
from ._typing import _TYPE_BODY, _TYPE_BODY_POSITION, _TYPE_TIMEOUT, ProxyConfig
from .backend import ConnectionInfo, ResponsePromise
from .connection import (
    BrokenPipeError,
    DummyConnection,
    HTTPConnection,
    HTTPSConnection,
    _wrap_proxy_error,
)
from .connection import port_by_scheme as port_by_scheme
from .contrib.resolver import (
    BaseResolver,
    ManyResolver,
    ProtocolResolver,
    ResolverDescription,
)
from .exceptions import (
    BaseSSLError,
    ClosedPoolError,
    EmptyPoolError,
    FullPoolError,
    HostChangedError,
    InsecureRequestWarning,
    LocationValueError,
    MaxRetryError,
    NewConnectionError,
    ProtocolError,
    ProxyError,
    ReadTimeoutError,
    SSLError,
    TimeoutError,
)
from .response import HTTPResponse
from .util.connection import is_connection_dropped
from .util.proxy import connection_requires_http_tunnel
from .util.request import NOT_FORWARDABLE_HEADERS, set_file_position
from .util.retry import Retry
from .util.ssl_match_hostname import CertificateError
from .util.timeout import _DEFAULT_TIMEOUT, Timeout
from .util.traffic_police import TrafficPolice, UnavailableTraffic
from .util.url import Url, _encode_target
from .util.url import _normalize_host as normalize_host
from .util.url import parse_url
from .util.util import to_str

if typing.TYPE_CHECKING:
    import ssl

    from typing_extensions import Literal

log = logging.getLogger(__name__)

_SelfT = typing.TypeVar("_SelfT")


# Pool objects
class ConnectionPool:
    """
    Base class for all connection pools, such as
    :class:`.HTTPConnectionPool` and :class:`.HTTPSConnectionPool`.

    .. note::
       ConnectionPool.urlopen() does not normalize or percent-encode target URIs
       which is useful if your target server doesn't support percent-encoded
       target URIs.
    """

    scheme: str | None = None
    QueueCls = TrafficPolice

    def __init__(self, host: str, port: int | None = None) -> None:
        if not host:
            raise LocationValueError("No host specified.")

        self.host = _normalize_host(host, scheme=self.scheme)
        self.port = port

        # This property uses 'normalize_host()' (not '_normalize_host()')
        # to avoid removing square braces around IPv6 addresses.
        # This value is sent to `HTTPConnection.set_tunnel()` if called
        # because square braces are required for HTTP CONNECT tunneling.
        self._tunnel_host = normalize_host(host, scheme=self.scheme).lower()

    def __str__(self) -> str:
        return f"{type(self).__name__}(host={self.host!r}, port={self.port!r})"

    def __enter__(self: _SelfT) -> _SelfT:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> Literal[False]:
        self.close()
        # Return False to re-raise any potential exceptions
        return False

    def close(self) -> None:
        """
        Close all pooled connections and disable the pool.
        """

    @property
    def is_idle(self) -> bool:
        raise NotImplementedError


# This is taken from http://hg.python.org/cpython/file/7aaba721ebc0/Lib/socket.py#l252
_blocking_errnos = {errno.EAGAIN, errno.EWOULDBLOCK}


class HTTPConnectionPool(ConnectionPool, RequestMethods):
    """
    Thread-safe connection pool for one host.

    :param host:
        Host used for this HTTP Connection (e.g. "localhost"), passed into
        :class:`http.client.HTTPConnection`.

    :param port:
        Port used for this HTTP Connection (None is equivalent to 80), passed
        into :class:`http.client.HTTPConnection`.

    :param timeout:
        Socket timeout in seconds for each individual connection. This can
        be a float or integer, which sets the timeout for the HTTP request,
        or an instance of :class:`urllib3.util.Timeout` which gives you more
        fine-grained control over request timeouts. After the constructor has
        been parsed, this is always a `urllib3.util.Timeout` object.

    :param maxsize:
        Number of connections to save that can be reused. More than 1 is useful
        in multithreaded situations. If ``block`` is set to False, more
        connections will be created but they will not be saved once they've
        been used.

    :param block:
        If set to True, no more than ``maxsize`` connections will be used at
        a time. When no free connections are available, the call will block
        until a connection has been released. This is a useful side effect for
        particular multithreaded situations where one does not want to use more
        than maxsize connections per host to prevent flooding.

    :param headers:
        Headers to include with all requests, unless other headers are given
        explicitly.

    :param retries:
        Retry configuration to use by default with requests in this pool.

    :param _proxy:
        Parsed proxy URL, should not be used directly, instead, see
        :class:`urllib3.ProxyManager`

    :param _proxy_headers:
        A dictionary with proxy headers, should not be used directly,
        instead, see :class:`urllib3.ProxyManager`

    :param \\**conn_kw:
        Additional parameters are used to create fresh :class:`urllib3.connection.HTTPConnection`,
        :class:`urllib3.connection.HTTPSConnection` instances.
    """

    scheme = "http"
    ConnectionCls: (type[HTTPConnection] | type[HTTPSConnection]) = HTTPConnection

    def __init__(
        self,
        host: str,
        port: int | None = None,
        timeout: _TYPE_TIMEOUT | None = _DEFAULT_TIMEOUT,
        maxsize: int = 1,
        block: bool = False,
        headers: typing.Mapping[str, str] | None = None,
        retries: Retry | bool | int | None = None,
        _proxy: Url | None = None,
        _proxy_headers: typing.Mapping[str, str] | None = None,
        _proxy_config: ProxyConfig | None = None,
        resolver: ResolverDescription
        | list[ResolverDescription]
        | str
        | list[str]
        | BaseResolver
        | None = None,
        happy_eyeballs: bool | int = False,
        **conn_kw: typing.Any,
    ):
        ConnectionPool.__init__(self, host, port)
        RequestMethods.__init__(self, headers)

        if not isinstance(timeout, Timeout):
            timeout = Timeout.from_float(timeout)

        if retries is None:
            retries = Retry.DEFAULT

        self.timeout = timeout
        self.retries = retries
        self.happy_eyeballs = happy_eyeballs

        self._maxsize = maxsize

        if self.QueueCls is not TrafficPolice and not issubclass(
            self.QueueCls, TrafficPolice
        ):
            warnings.warn(
                "ConnectionPool QueueCls no longer support typical queue implementation "
                "due to its inability to answer urllib3.future needs to handle concurrent streams "
                "in a single connection. You may customize the implementation by passing a subclass of "
                "urllib3.util.traffic_police.TrafficPolice if necessary.",
                DeprecationWarning,
            )
            self.QueueCls = TrafficPolice

        self.pool: TrafficPolice[HTTPConnection] | None = self.QueueCls(maxsize)
        self.block = block

        self.proxy = _proxy
        self.proxy_headers = _proxy_headers or {}
        self.proxy_config = _proxy_config

        # These are mostly for testing and debugging purposes.
        self.num_connections = 0
        self.num_requests = 0
        self.conn_kw = conn_kw

        if self.proxy:
            # Enable Nagle's algorithm for proxies, to avoid packet fragmentation.
            # We cannot know if the user has added default socket options, so we cannot replace the
            # list.
            self.conn_kw.setdefault("socket_options", [])

            self.conn_kw["proxy"] = self.proxy
            self.conn_kw["proxy_config"] = self.proxy_config

        self._own_resolver = not isinstance(resolver, BaseResolver)

        if resolver is None:
            resolver = [ResolverDescription(ProtocolResolver.SYSTEM)]
        elif isinstance(resolver, str):
            resolver = [ResolverDescription.from_url(resolver)]
        elif isinstance(resolver, ResolverDescription):
            resolver = [resolver]

        self._resolvers: list[ResolverDescription] = []

        if not isinstance(resolver, BaseResolver):
            can_resolve_localhost: bool = False

            for resolver_description in resolver:
                if isinstance(resolver_description, str):
                    self._resolvers.append(
                        ResolverDescription.from_url(resolver_description)
                    )

                    if self._resolvers[-1].protocol == ProtocolResolver.SYSTEM:
                        can_resolve_localhost = True
                    continue

                self._resolvers.append(resolver_description)

                if self._resolvers[-1].protocol == ProtocolResolver.SYSTEM:
                    can_resolve_localhost = True

            if not can_resolve_localhost:
                self._resolvers.append(
                    ResolverDescription.from_url("system://default?hosts=localhost")
                )

            #: We want to automatically forward ca_cert_data, ca_cert_dir, and ca_certs.
            for rd in self._resolvers:
                if "ca_cert_data" in conn_kw:
                    if "ca_cert_data" not in rd:
                        rd["ca_cert_data"] = conn_kw["ca_cert_data"]
                if "ca_cert_dir" in conn_kw:
                    if "ca_cert_dir" not in rd:
                        rd["ca_cert_dir"] = conn_kw["ca_cert_dir"]
                if "ca_certs" in conn_kw:
                    if "ca_certs" not in rd:
                        rd["ca_certs"] = conn_kw["ca_certs"]

        self._resolver: BaseResolver = (
            ManyResolver(*[r.new() for r in self._resolvers])
            if not isinstance(resolver, BaseResolver)
            else resolver
        )

        self.conn_kw["resolver"] = self._resolver

    @property
    def is_idle(self) -> bool:
        return self.pool is None or self.pool.bag_only_idle

    def _new_conn(self, *, heb_timeout: Timeout | None = None) -> HTTPConnection:
        """
        Return a fresh :class:`HTTPConnection`.
        """
        if self.pool is None:
            raise ClosedPoolError(self, "Pool is closed")

        self.num_connections += 1
        log.debug(
            "Starting new HTTP connection (%d): %s:%s",
            self.num_connections,
            self.host,
            self.port or "80",
        )

        conn = None

        if self.happy_eyeballs:
            log.debug(
                "Attempting Happy-Eyeball %s:%s",
                self.host,
                self.port or "443",
            )

            dt_pre_resolve = datetime.now(tz=timezone.utc)
            ip_addresses = self._resolver.getaddrinfo(
                self.host,
                self.port,
                socket.AF_UNSPEC
                if "socket_family" not in self.conn_kw
                else self.conn_kw["socket_family"],
                socket.SOCK_STREAM,
                quic_upgrade_via_dns_rr=False,
            )
            delta_post_resolve = datetime.now(tz=timezone.utc) - dt_pre_resolve

            if len(ip_addresses) > 1:
                ipv6_addresses = []
                ipv4_addresses = []

                for ip_address in ip_addresses:
                    if ip_address[0] == socket.AF_INET6:
                        ipv6_addresses.append(ip_address)
                    else:
                        ipv4_addresses.append(ip_address)

                if ipv4_addresses and ipv6_addresses:
                    log.debug(
                        "Happy-Eyeball Dual-Stack %s:%s",
                        self.host,
                        self.port or "443",
                    )

                    intermediary_addresses = []
                    for ipv6_entry, ipv4_entry in zip_longest(
                        ipv6_addresses, ipv4_addresses
                    ):
                        if ipv6_entry:
                            intermediary_addresses.append(ipv6_entry)
                        if ipv4_entry:
                            intermediary_addresses.append(ipv4_entry)
                    ip_addresses = intermediary_addresses
                else:
                    log.debug(
                        "Happy-Eyeball Single-Stack %s:%s",
                        self.host,
                        self.port or "443",
                    )

                challengers = []
                max_task = (
                    4 if isinstance(self.happy_eyeballs, bool) else self.happy_eyeballs
                )

                if heb_timeout is None:
                    heb_timeout = self.timeout

                override_timeout = (
                    heb_timeout.connect_timeout
                    if heb_timeout.connect_timeout is not None
                    and isinstance(heb_timeout.connect_timeout, (float, int))
                    else 0.4
                )

                for ip_address in ip_addresses[:max_task]:
                    conn_kw = self.conn_kw.copy()
                    target_solo_addr = (
                        f"[{ip_address[-1][0]}]"
                        if ip_address[0] == socket.AF_INET6
                        else ip_address[-1][0]
                    )
                    conn_kw["resolver"] = ResolverDescription.from_url(
                        f"in-memory://default?hosts={self.host}:{target_solo_addr}"
                    ).new()
                    conn_kw["socket_family"] = ip_address[0]

                    challengers.append(
                        self.ConnectionCls(
                            host=self.host,
                            port=self.port,
                            timeout=override_timeout,
                            **conn_kw,
                        )
                    )

                event = threading.Event()
                winning_task: Future[None] | None = None
                completed_count: int = 0

                def _happy_eyeballs_completed(t: Future[None]) -> None:
                    nonlocal winning_task, event, completed_count

                    if winning_task is None and t.exception() is None:
                        winning_task = t
                        event.set()

                        return

                    completed_count += 1

                    if completed_count >= len(challengers):
                        event.set()

                tpe = ThreadPoolExecutor(max_workers=max_task)

                tasks: list[Future[None]] = []

                for challenger in challengers:
                    task = tpe.submit(challenger.connect)
                    task.add_done_callback(_happy_eyeballs_completed)

                    tasks.append(task)

                event.wait()

                for task in tasks:
                    if task == winning_task:
                        continue

                    if task.running():
                        task.cancel()
                    else:
                        challengers[tasks.index(task)].close()

                if winning_task is None:
                    within_delay_msg: str = (
                        f" within {override_timeout}s" if override_timeout else ""
                    )
                    raise NewConnectionError(
                        challengers[0],
                        f"Failed to establish a new connection: No suitable address to connect to using Happy Eyeballs for {self.host}:{self.port}{within_delay_msg}",
                    ) from tasks[0].exception()

                conn = challengers[tasks.index(winning_task)]

                # we have to replace the resolution latency metric
                if conn.conn_info:
                    conn.conn_info.resolution_latency = delta_post_resolve

                tpe.shutdown(wait=False)
            else:
                log.debug(
                    "Happy-Eyeball Ineligible %s:%s",
                    self.host,
                    self.port or "443",
                )

        if conn is None:
            conn = self.ConnectionCls(
                host=self.host,
                port=self.port,
                timeout=self.timeout.connect_timeout,
                **self.conn_kw,
            )
        self.pool.put(conn, immediately_unavailable=True)
        return conn

    def _get_conn(
        self, timeout: float | None = None, *, heb_timeout: Timeout | None = None
    ) -> HTTPConnection:
        """
        Get a connection. Will return a pooled connection if one is available.

        If no connections are available and :prop:`.block` is ``False``, then a
        fresh connection is returned.

        :param timeout:
            Seconds to wait before giving up and raising
            :class:`urllib3.exceptions.EmptyPoolError` if the pool is empty and
            :prop:`.block` is ``True``.
        """
        conn = None

        if self.pool is None:
            raise ClosedPoolError(self, "Pool is closed.")

        try:
            conn = self.pool.get(
                block=self.block, timeout=timeout, non_saturated_only=True
            )
        except AttributeError:  # self.pool is None
            raise ClosedPoolError(self, "Pool is closed.") from None  # Defensive:

        except queue.Empty:
            if self.block:
                raise EmptyPoolError(
                    self,
                    "Pool is empty and a new connection can't be opened due to blocking mode.",
                ) from None
            pass  # Oh well, we'll create a new connection then

        # If this is a persistent connection, check if it got disconnected
        if conn and is_connection_dropped(conn):
            log.debug("Resetting dropped connection: %s", self.host)
            conn.close()

        try:
            return conn or self._new_conn(heb_timeout=heb_timeout)
        except (
            TypeError
        ):  # this branch catch overridden pool that don't support Happy-Eyeballs!
            conn = self._new_conn()
            # as this branch is meant for people bypassing our main logic, we have to memorize the conn immediately
            # into our pool of conn.
            self.pool.put(conn, immediately_unavailable=True)
            return conn

    def _put_conn(self, conn: HTTPConnection) -> None:
        """
        Put a connection back into the pool.

        :param conn:
            Connection object for the current host and port as returned by
            :meth:`._new_conn` or :meth:`._get_conn`.

        If the pool is already full, the connection is closed and discarded
        because we exceeded maxsize. If connections are discarded frequently,
        then maxsize should be increased.

        If the pool is closed, then the connection will be closed and discarded.
        """

        if self.pool is not None:
            try:
                self.pool.put(conn, block=False)
                return  # Everything is dandy, done.
            except AttributeError:
                # self.pool is None.
                pass
            except queue.Full:
                # Connection never got put back into the pool, close it.
                if conn:
                    if conn.is_idle:
                        conn.close()

                if self.block:
                    # This should never happen if you got the conn from self._get_conn
                    raise FullPoolError(
                        self,
                        "Pool reached maximum size and no more connections are allowed.",
                    ) from None
                else:
                    # multiplexed connection may still have in-flight request not converted into response
                    # we shall not discard it until responses are consumed.
                    if conn and conn.is_idle is False:
                        log.warning(
                            "Connection pool is full, temporary increase, keeping connection, "
                            "multiplexed and not idle: %s. Connection pool size: %s",
                            self.host,
                            self.pool.qsize(),
                        )

                        if self.pool.maxsize is not None:
                            self.pool.maxsize += 1
                        return self._put_conn(conn)

                log.warning(
                    "Connection pool is full, discarding connection: %s. Connection pool size: %s",
                    self.host,
                    self.pool.qsize(),
                )
                self.num_connections -= 1
                return

        # Connection never got put back into the pool, close it.
        if conn:
            conn.close()
            self.num_connections -= 1

    def _validate_conn(self, conn: HTTPConnection) -> None:
        """
        Called right before a request is made, after the socket is created.
        """
        if conn.is_closed:
            conn.connect()

    def _prepare_proxy(self, conn: HTTPConnection) -> None:
        # Nothing to do for HTTP connections.
        pass

    def _get_timeout(self, timeout: _TYPE_TIMEOUT) -> Timeout:
        """Helper that always returns a :class:`urllib3.util.Timeout`"""
        if timeout is _DEFAULT_TIMEOUT:
            return self.timeout.clone()

        if isinstance(timeout, Timeout):
            return timeout.clone()
        else:
            # User passed us an int/float. This is for backwards compatibility,
            # can be removed later
            return Timeout.from_float(timeout)

    def _raise_timeout(
        self,
        err: BaseSSLError | OSError | SocketTimeout,
        url: str,
        timeout_value: _TYPE_TIMEOUT | None,
    ) -> None:
        """Is the error actually a timeout? Will raise a ReadTimeout or pass"""

        if isinstance(err, SocketTimeout):
            raise ReadTimeoutError(
                self, url, f"Read timed out. (read timeout={timeout_value})"
            ) from err

        # See the above comment about EAGAIN in Python 3.
        if hasattr(err, "errno") and err.errno in _blocking_errnos:
            raise ReadTimeoutError(
                self, url, f"Read timed out. (read timeout={timeout_value})"
            ) from err

    def get_response(
        self, *, promise: ResponsePromise | None = None
    ) -> HTTPResponse | None:
        """
        Retrieve the first response available in the pool.
        This method should be called after issuing at least one request with ``multiplexed=True``.
        If none available, return None.
        """
        if self.pool is None:
            raise ClosedPoolError(self, "Pool is closed")

        if promise is not None and not isinstance(promise, ResponsePromise):
            raise TypeError(
                f"get_response only support ResponsePromise but received {type(promise)} instead. "
                f"This may occur if you expected the remote peer to support multiplexing but did not."
            )

        try:
            with self.pool.borrow(
                promise or ResponsePromise,
                block=promise is not None,
                not_idle_only=promise is None,
            ) as conn:
                response = conn.getresponse(promise=promise, police_officer=self.pool)
        except UnavailableTraffic:
            return None

        if promise is not None and response is None:
            raise ValueError(
                "Invoked get_response with promise=... that no connection in pool recognize"
            )

        if response is None:
            return None

        from_promise = None

        if promise:
            from_promise = promise
        else:
            if (
                response._fp
                and hasattr(response._fp, "from_promise")
                and response._fp.from_promise
            ):
                from_promise = response._fp.from_promise

        if from_promise is None:
            raise ValueError(
                "Internal: Unable to identify originating ResponsePromise from a LowLevelResponse"
            )

        self.pool.forget(from_promise)

        # Retrieve request ctx
        method = typing.cast(str, from_promise.get_parameter("method"))
        redirect = typing.cast(bool, from_promise.get_parameter("redirect"))

        # Handle redirect?
        redirect_location = redirect and response.get_redirect_location()
        if redirect_location:
            url = typing.cast(str, from_promise.get_parameter("url"))
            body = typing.cast(
                typing.Optional[_TYPE_BODY], from_promise.get_parameter("body")
            )
            headers = typing.cast(HTTPHeaderDict, from_promise.get_parameter("headers"))
            preload_content = typing.cast(
                bool, from_promise.get_parameter("preload_content")
            )
            decode_content = typing.cast(
                bool, from_promise.get_parameter("decode_content")
            )
            timeout = typing.cast(
                typing.Optional[_TYPE_TIMEOUT], from_promise.get_parameter("timeout")
            )
            assert_same_host = typing.cast(
                bool, from_promise.get_parameter("assert_same_host")
            )
            pool_timeout = from_promise.get_parameter("pool_timeout")
            response_kw = typing.cast(
                typing.MutableMapping[str, typing.Any],
                from_promise.get_parameter("response_kw"),
            )
            chunked = typing.cast(bool, from_promise.get_parameter("chunked"))
            body_pos = typing.cast(
                _TYPE_BODY_POSITION, from_promise.get_parameter("body_pos")
            )
            retries = typing.cast(Retry, from_promise.get_parameter("retries"))

            if response.status == 303:
                method = "GET"
                body = None
                headers = HTTPHeaderDict(headers)

                for should_be_removed_header in NOT_FORWARDABLE_HEADERS:
                    headers.discard(should_be_removed_header)

            try:
                retries = retries.increment(method, url, response=response, _pool=self)
            except MaxRetryError:
                if retries.raise_on_redirect:
                    response.drain_conn()
                    raise
                return response

            response.drain_conn()
            retries.sleep_for_retry(response)
            log.debug("Redirecting %s -> %s", url, redirect_location)
            new_promise = self.urlopen(
                method,
                redirect_location,
                body,
                headers,
                retries=retries,
                redirect=redirect,
                assert_same_host=assert_same_host,
                timeout=timeout,
                pool_timeout=pool_timeout,
                release_conn=True,
                chunked=chunked,
                body_pos=body_pos,
                preload_content=preload_content,
                decode_content=decode_content,
                multiplexed=True,
                **response_kw,
            )

            return self.get_response(promise=new_promise if promise else None)

        # Check if we should retry the HTTP response.
        has_retry_after = bool(response.headers.get("Retry-After"))
        retries = typing.cast(Retry, from_promise.get_parameter("retries"))

        if retries.is_retry(method, response.status, has_retry_after):
            url = typing.cast(str, from_promise.get_parameter("url"))
            body = typing.cast(
                typing.Optional[_TYPE_BODY], from_promise.get_parameter("body")
            )
            headers = typing.cast(HTTPHeaderDict, from_promise.get_parameter("headers"))
            preload_content = typing.cast(
                bool, from_promise.get_parameter("preload_content")
            )
            decode_content = typing.cast(
                bool, from_promise.get_parameter("decode_content")
            )
            timeout = typing.cast(
                typing.Optional[_TYPE_TIMEOUT], from_promise.get_parameter("timeout")
            )
            assert_same_host = typing.cast(
                bool, from_promise.get_parameter("assert_same_host")
            )
            pool_timeout = from_promise.get_parameter("pool_timeout")
            response_kw = typing.cast(
                typing.MutableMapping[str, typing.Any],
                from_promise.get_parameter("response_kw"),
            )
            chunked = typing.cast(bool, from_promise.get_parameter("chunked"))
            body_pos = typing.cast(
                _TYPE_BODY_POSITION, from_promise.get_parameter("body_pos")
            )

            try:
                retries = retries.increment(method, url, response=response, _pool=self)
            except MaxRetryError:
                if retries.raise_on_status:
                    response.drain_conn()
                    raise
                return response

            response.drain_conn()
            retries.sleep(response)
            log.debug("Retry: %s", url)
            new_promise = self.urlopen(
                method,
                url,
                body,
                headers,
                retries=retries,
                redirect=redirect,
                assert_same_host=assert_same_host,
                timeout=timeout,
                pool_timeout=pool_timeout,
                release_conn=False,
                chunked=chunked,
                body_pos=body_pos,
                preload_content=preload_content,
                decode_content=decode_content,
                multiplexed=True,
                **response_kw,
            )

            return self.get_response(promise=new_promise if promise else None)

        return response

    @typing.overload
    def _make_request(
        self,
        conn: HTTPConnection,
        method: str,
        url: str,
        body: _TYPE_BODY | None = ...,
        headers: typing.Mapping[str, str] | None = ...,
        retries: Retry | None = ...,
        timeout: _TYPE_TIMEOUT = ...,
        chunked: bool = ...,
        response_conn: HTTPConnection | None = ...,
        preload_content: bool = ...,
        decode_content: bool = ...,
        enforce_content_length: bool = ...,
        on_post_connection: typing.Callable[[ConnectionInfo], None] | None = ...,
        on_upload_body: typing.Callable[[int, int | None, bool, bool], None] = ...,
        *,
        multiplexed: Literal[True],
    ) -> ResponsePromise:
        ...

    @typing.overload
    def _make_request(
        self,
        conn: HTTPConnection,
        method: str,
        url: str,
        body: _TYPE_BODY | None = ...,
        headers: typing.Mapping[str, str] | None = ...,
        retries: Retry | None = ...,
        timeout: _TYPE_TIMEOUT = ...,
        chunked: bool = ...,
        response_conn: HTTPConnection | None = ...,
        preload_content: bool = ...,
        decode_content: bool = ...,
        enforce_content_length: bool = ...,
        on_post_connection: typing.Callable[[ConnectionInfo], None] | None = ...,
        on_upload_body: typing.Callable[[int, int | None, bool, bool], None] = ...,
        *,
        multiplexed: Literal[False] = ...,
    ) -> HTTPResponse:
        ...

    def _make_request(
        self,
        conn: HTTPConnection,
        method: str,
        url: str,
        body: _TYPE_BODY | None = None,
        headers: typing.Mapping[str, str] | None = None,
        retries: Retry | None = None,
        timeout: _TYPE_TIMEOUT = _DEFAULT_TIMEOUT,
        chunked: bool = False,
        response_conn: HTTPConnection | None = None,
        preload_content: bool = True,
        decode_content: bool = True,
        enforce_content_length: bool = True,
        on_post_connection: typing.Callable[[ConnectionInfo], None] | None = None,
        on_upload_body: typing.Callable[[int, int | None, bool, bool], None]
        | None = None,
        multiplexed: Literal[False] | Literal[True] = False,
    ) -> HTTPResponse | ResponsePromise:
        """
        Perform a request on a given urllib connection object taken from our
        pool.

        :param conn:
            a connection from one of our connection pools

        :param method:
            HTTP request method (such as GET, POST, PUT, etc.)

        :param url:
            The URL to perform the request on.

        :param body:
            Data to send in the request body, either :class:`str`, :class:`bytes`,
            an iterable of :class:`str`/:class:`bytes`, or a file-like object.

        :param headers:
            Dictionary of custom headers to send, such as User-Agent,
            If-None-Match, etc. If None, pool headers are used. If provided,
            these headers completely replace any pool-specific headers.

        :param retries:
            Configure the number of retries to allow before raising a
            :class:`~urllib3.exceptions.MaxRetryError` exception.

            Pass ``None`` to retry until you receive a response. Pass a
            :class:`~urllib3.util.retry.Retry` object for fine-grained control
            over different types of retries.
            Pass an integer number to retry connection errors that many times,
            but no other types of errors. Pass zero to never retry.

            If ``False``, then retries are disabled and any exception is raised
            immediately. Also, instead of raising a MaxRetryError on redirects,
            the redirect response will be returned.

        :type retries: :class:`~urllib3.util.retry.Retry`, False, or an int.

        :param timeout:
            If specified, overrides the default timeout for this one
            request. It may be a float (in seconds) or an instance of
            :class:`urllib3.util.Timeout`.

        :param chunked:
            If True, urllib3 will send the body using chunked transfer
            encoding. Otherwise, urllib3 will send the body using the standard
            content-length form. Defaults to False.

        :param response_conn:
            Set this to ``None`` if you will handle releasing the connection or
            set the connection to have the response release it.

        :param preload_content:
          If True, the response's body will be preloaded during construction.

        :param decode_content:
            If True, will attempt to decode the body based on the
            'content-encoding' header.

        :param enforce_content_length:
            Enforce content length checking. Body returned by server must match
            value of Content-Length header, if present. Otherwise, raise error.
        """
        self.num_requests += 1

        timeout_obj = self._get_timeout(timeout)
        timeout_obj.start_connect()
        conn.timeout = Timeout.resolve_default_timeout(timeout_obj.connect_timeout)

        try:
            # Trigger any extra validation we need to do.
            try:
                self._validate_conn(conn)
            except (SocketTimeout, BaseSSLError) as e:
                self._raise_timeout(err=e, url=url, timeout_value=conn.timeout)
                raise

        # _validate_conn() starts the connection to an HTTPS proxy
        # so we need to wrap errors with 'ProxyError' here too.
        except (
            OSError,
            NewConnectionError,
            TimeoutError,
            BaseSSLError,
            CertificateError,
            SSLError,
        ) as e:
            new_e: Exception = e
            if isinstance(e, (BaseSSLError, CertificateError)):
                new_e = SSLError(e)
            # If the connection didn't successfully connect to it's proxy
            # then there
            if isinstance(
                new_e, (OSError, NewConnectionError, TimeoutError, SSLError)
            ) and (conn and conn.proxy and not conn.has_connected_to_proxy):
                new_e = _wrap_proxy_error(new_e, conn.proxy.scheme)
            raise new_e

        if on_post_connection is not None and conn.conn_info is not None:
            # A second request does not redo handshake or DNS resolution.
            if (
                hasattr(conn, "_start_last_request")
                and conn._start_last_request is not None
            ):
                if conn.conn_info.tls_handshake_latency:
                    conn.conn_info.tls_handshake_latency = timedelta()
                if conn.conn_info.established_latency:
                    conn.conn_info.established_latency = timedelta()
                if conn.conn_info.resolution_latency:
                    conn.conn_info.resolution_latency = timedelta()
                if conn.conn_info.request_sent_latency:
                    conn.conn_info.request_sent_latency = None
            on_post_connection(conn.conn_info)

        if conn.is_multiplexed is False and multiplexed is True:
            # overruling
            multiplexed = False

        try:
            rp = conn.request(
                method,
                url,
                body=body,
                headers=headers,
                chunked=chunked,
                preload_content=preload_content,
                decode_content=decode_content,
                enforce_content_length=enforce_content_length,
                on_upload_body=on_upload_body,
            )
        # We are swallowing BrokenPipeError (errno.EPIPE) since the server is
        # legitimately able to close the connection after sending a valid response.
        # With this behaviour, the received response is still readable.
        except BrokenPipeError as e:
            rp = e.promise  # type: ignore
        except OSError as e:
            rp = None
            # MacOS/Linux
            # EPROTOTYPE is needed on macOS
            # https://erickt.github.io/blog/2014/11/19/adventures-in-debugging-a-potential-osx-kernel-bug/
            if e.errno != errno.EPROTOTYPE:
                raise

        # Reset the timeout for the recv() on the socket
        read_timeout = timeout_obj.read_timeout

        if multiplexed:
            if rp is None:
                raise OSError
            rp.set_parameter("read_timeout", read_timeout)
            return rp

        if not conn.is_closed:
            # In Python 3 socket.py will catch EAGAIN and return None when you
            # try and read into the file pointer created by http.client, which
            # instead raises a BadStatusLine exception. Instead of catching
            # the exception and assuming all BadStatusLine exceptions are read
            # timeouts, check for a zero timeout before making the request.
            if read_timeout == 0:
                raise ReadTimeoutError(
                    self, url, f"Read timed out. (read timeout={read_timeout})"
                )
            conn.timeout = read_timeout

        # Receive the response from the server
        try:
            response = conn.getresponse(police_officer=self.pool)
        except (BaseSSLError, OSError) as e:
            self._raise_timeout(err=e, url=url, timeout_value=read_timeout)
            raise

        # Set properties that are used by the pooling layer.
        response.retries = retries
        response._pool = self

        log.debug(
            '%s://%s:%s "%s %s %s" %s %s',
            self.scheme,
            self.host,
            self.port,
            method,
            url,
            # HTTP version
            conn._http_vsn_str,
            response.status,
            response.length_remaining,
        )

        return response

    def close(self) -> None:
        """
        Close all pooled connections and disable the pool.
        """
        if self.pool is None:
            return

        # Disable access to the pool
        old_pool, self.pool = self.pool, None

        # Close all the HTTPConnections in the pool.
        old_pool.clear()

        # Close allocated resolver if we own it. (aka. not shared)
        if self._own_resolver and self._resolver.is_available():
            self._resolver.close()

    def is_same_host(self, url: str) -> bool:
        """
        Check if the given ``url`` is a member of the same host as this
        connection pool.
        """
        if url.startswith("/"):
            return True

        # TODO: Add optional support for socket.gethostbyname checking.
        scheme, _, host, port, *_ = parse_url(url)
        scheme = scheme or "http"
        if host is not None:
            host = _normalize_host(host, scheme=scheme)

        # Use explicit default port for comparison when none is given
        if self.port and not port:
            port = port_by_scheme.get(scheme)
        elif not self.port and port == port_by_scheme.get(scheme):
            port = None

        return (scheme, host, port) == (self.scheme, self.host, self.port)

    @typing.overload  # type: ignore[override]
    def urlopen(
        self,
        method: str,
        url: str,
        body: _TYPE_BODY | None = ...,
        headers: typing.Mapping[str, str] | None = ...,
        retries: Retry | bool | int | None = ...,
        redirect: bool = ...,
        assert_same_host: bool = ...,
        timeout: _TYPE_TIMEOUT = ...,
        pool_timeout: int | None = ...,
        release_conn: bool | None = ...,
        chunked: bool = ...,
        body_pos: _TYPE_BODY_POSITION | None = ...,
        preload_content: bool = ...,
        decode_content: bool = ...,
        on_post_connection: typing.Callable[[ConnectionInfo], None] | None = ...,
        on_upload_body: typing.Callable[[int, int | None, bool, bool], None] = ...,
        *,
        multiplexed: Literal[False] = ...,
        **response_kw: typing.Any,
    ) -> HTTPResponse:
        ...

    @typing.overload
    def urlopen(
        self,
        method: str,
        url: str,
        body: _TYPE_BODY | None = ...,
        headers: typing.Mapping[str, str] | None = ...,
        retries: Retry | bool | int | None = ...,
        redirect: bool = ...,
        assert_same_host: bool = ...,
        timeout: _TYPE_TIMEOUT = ...,
        pool_timeout: int | None = ...,
        release_conn: bool | None = ...,
        chunked: bool = ...,
        body_pos: _TYPE_BODY_POSITION | None = ...,
        preload_content: bool = ...,
        decode_content: bool = ...,
        on_post_connection: typing.Callable[[ConnectionInfo], None] | None = ...,
        on_upload_body: typing.Callable[[int, int | None, bool, bool], None] = ...,
        *,
        multiplexed: Literal[True],
        **response_kw: typing.Any,
    ) -> ResponsePromise:
        ...

    def urlopen(
        self,
        method: str,
        url: str,
        body: _TYPE_BODY | None = None,
        headers: typing.Mapping[str, str] | None = None,
        retries: Retry | bool | int | None = None,
        redirect: bool = True,
        assert_same_host: bool = True,
        timeout: _TYPE_TIMEOUT = _DEFAULT_TIMEOUT,
        pool_timeout: int | None = None,
        release_conn: bool | None = None,
        chunked: bool = False,
        body_pos: _TYPE_BODY_POSITION | None = None,
        preload_content: bool = True,
        decode_content: bool = True,
        on_post_connection: typing.Callable[[ConnectionInfo], None] | None = None,
        on_upload_body: typing.Callable[[int, int | None, bool, bool], None]
        | None = None,
        multiplexed: bool = False,
        **response_kw: typing.Any,
    ) -> HTTPResponse | ResponsePromise:
        """
        Get a connection from the pool and perform an HTTP request. This is the
        lowest level call for making a request, so you'll need to specify all
        the raw details.

        .. note::

           More commonly, it's appropriate to use a convenience method
           such as :meth:`request`.

        .. note::

           `release_conn` will only behave as expected if
           `preload_content=False` because we want to make
           `preload_content=False` the default behaviour someday soon without
           breaking backwards compatibility.

        :param method:
            HTTP request method (such as GET, POST, PUT, etc.)

        :param url:
            The URL to perform the request on.

        :param body:
            Data to send in the request body, either :class:`str`, :class:`bytes`,
            an iterable of :class:`str`/:class:`bytes`, or a file-like object.

        :param headers:
            Dictionary of custom headers to send, such as User-Agent,
            If-None-Match, etc. If None, pool headers are used. If provided,
            these headers completely replace any pool-specific headers.

        :param retries:
            Configure the number of retries to allow before raising a
            :class:`~urllib3.exceptions.MaxRetryError` exception.

            Pass ``None`` to retry until you receive a response. Pass a
            :class:`~urllib3.util.retry.Retry` object for fine-grained control
            over different types of retries.
            Pass an integer number to retry connection errors that many times,
            but no other types of errors. Pass zero to never retry.

            If ``False``, then retries are disabled and any exception is raised
            immediately. Also, instead of raising a MaxRetryError on redirects,
            the redirect response will be returned.

        :type retries: :class:`~urllib3.util.retry.Retry`, False, or an int.

        :param redirect:
            If True, automatically handle redirects (status codes 301, 302,
            303, 307, 308). Each redirect counts as a retry. Disabling retries
            will disable redirect, too.

        :param assert_same_host:
            If ``True``, will make sure that the host of the pool requests is
            consistent else will raise HostChangedError. When ``False``, you can
            use the pool on an HTTP proxy and request foreign hosts.

        :param timeout:
            If specified, overrides the default timeout for this one
            request. It may be a float (in seconds) or an instance of
            :class:`urllib3.util.Timeout`.

        :param pool_timeout:
            If set and the pool is set to block=True, then this method will
            block for ``pool_timeout`` seconds and raise EmptyPoolError if no
            connection is available within the time period.

        :param bool preload_content:
            If True, the response's body will be preloaded into memory.

        :param bool decode_content:
            If True, will attempt to decode the body based on the
            'content-encoding' header.

        :param release_conn:
            If False, then the urlopen call will not release the connection
            back into the pool once a response is received (but will release if
            you read the entire contents of the response such as when
            `preload_content=True`). This is useful if you're not preloading
            the response's content immediately. You will need to call
            ``r.release_conn()`` on the response ``r`` to return the connection
            back into the pool. If None, it takes the value of ``preload_content``
            which defaults to ``True``.

        :param bool chunked:
            If True, urllib3 will send the body using chunked transfer
            encoding. Otherwise, urllib3 will send the body using the standard
            content-length form. Defaults to False.

        :param int body_pos:
            Position to seek to in file-like body in the event of a retry or
            redirect. Typically this won't need to be set because urllib3 will
            auto-populate the value when needed.

        :param on_post_connection:
            Callable to be invoked that will inform you of the connection specifications
            for the request to be sent. See ``urllib3.ConnectionInfo`` class for more.

        :param on_upload_body:
            Callable that will be invoked upon body upload in order to be able to track
            the progress. The values are expressed in bytes. It is possible that the total isn't
            available, thus set to None. In order, arguments are:
            (total_sent, total_to_be_sent, completed, any_error)

        :param multiplexed:
            Dispatch the request in a non-blocking way, this means that the
            response will be retrieved in the future with the get_response()
            method.
        """
        parsed_url = parse_url(url)
        destination_scheme = parsed_url.scheme

        if headers is None:
            headers = self.headers

        if not isinstance(retries, Retry):
            retries = Retry.from_int(retries, redirect=redirect, default=self.retries)

        if release_conn is None:
            release_conn = preload_content

        # Check host
        if assert_same_host and not self.is_same_host(url):
            raise HostChangedError(self, url, retries)

        # Ensure that the URL we're connecting to is properly encoded
        if url.startswith("/"):
            url = to_str(_encode_target(url))
        else:
            url = to_str(parsed_url.url)

        conn = None

        # Track whether `conn` needs to be released before
        # returning/raising/recursing. Update this variable if necessary, and
        # leave `release_conn` constant throughout the function. That way, if
        # the function recurses, the original value of `release_conn` will be
        # passed down into the recursive call, and its value will be respected.
        #
        # See issue #651 [1] for details.
        #
        # [1] <https://github.com/urllib3/urllib3/issues/651>
        release_this_conn = release_conn

        http_tunnel_required = connection_requires_http_tunnel(
            self.proxy, self.proxy_config, destination_scheme
        )

        # Merge the proxy headers. Only done when not using HTTP CONNECT. We
        # have to copy the headers dict so we can safely change it without those
        # changes being reflected in anyone else's copy.
        if not http_tunnel_required:
            headers = headers.copy()  # type: ignore[attr-defined]
            headers.update(self.proxy_headers)  # type: ignore[union-attr]

        # Must keep the exception bound to a separate variable or else Python 3
        # complains about UnboundLocalError.
        err = None

        # Keep track of whether we cleanly exited the except block. This
        # ensures we do proper cleanup in finally.
        clean_exit = False

        # Rewind body position, if needed. Record current position
        # for future rewinds in the event of a redirect/retry.
        body_pos = set_file_position(body, body_pos)

        try:
            # Request a connection from the queue.
            timeout_obj = self._get_timeout(timeout)
            conn = self._get_conn(timeout=pool_timeout, heb_timeout=timeout_obj)

            conn.timeout = timeout_obj.connect_timeout  # type: ignore[assignment]

            # Is this a closed/new connection that requires CONNECT tunnelling?
            if self.proxy is not None and http_tunnel_required and conn.is_closed:
                try:
                    self._prepare_proxy(conn)
                except (BaseSSLError, OSError, SocketTimeout) as e:
                    self._raise_timeout(
                        err=e, url=self.proxy.url, timeout_value=conn.timeout
                    )
                    raise

            # If we're going to release the connection in ``finally:``, then
            # the response doesn't need to know about the connection. Otherwise
            # it will also try to release it and we'll have a double-release
            # mess.
            response_conn = conn if not release_conn else None

            # Make the request on the HTTPConnection object
            response = self._make_request(  # type: ignore[call-overload,misc]
                conn,
                method,
                url,
                body=body,
                headers=headers,
                retries=retries,
                timeout=timeout_obj,
                chunked=chunked,
                response_conn=response_conn,
                preload_content=preload_content,
                decode_content=decode_content,
                enforce_content_length=True,
                on_post_connection=on_post_connection,
                on_upload_body=on_upload_body,
                multiplexed=multiplexed,
            )

            # it was established a non-multiplexed connection. fallback to original behavior.
            if not isinstance(response, ResponsePromise):
                multiplexed = False

            if multiplexed:
                response.set_parameter("method", method)
                response.set_parameter("url", url)
                response.set_parameter("body", body)
                response.set_parameter("headers", headers)
                response.set_parameter("retries", retries)
                response.set_parameter("preload_content", preload_content)
                response.set_parameter("decode_content", decode_content)
                response.set_parameter("timeout", timeout_obj)
                response.set_parameter("redirect", redirect)
                response.set_parameter("response_kw", response_kw)
                response.set_parameter("pool_timeout", pool_timeout)
                response.set_parameter("assert_same_host", assert_same_host)
                response.set_parameter("chunked", chunked)
                response.set_parameter("body_pos", body_pos)
                release_this_conn = True if not conn.is_saturated else False

            # Everything went great!
            clean_exit = True

        except EmptyPoolError:
            # Didn't get a connection from the pool, no need to clean up
            clean_exit = True
            release_this_conn = False
            raise

        except (
            TimeoutError,
            OSError,
            ProtocolError,
            BaseSSLError,
            SSLError,
            CertificateError,
            ProxyError,
        ) as e:
            # Discard the connection for these exceptions. It will be
            # replaced during the next _get_conn() call.
            clean_exit = False
            new_e: Exception = e
            if isinstance(e, (BaseSSLError, CertificateError)):
                new_e = SSLError(e)
            if isinstance(
                new_e,
                (
                    OSError,
                    NewConnectionError,
                    TimeoutError,
                    SSLError,
                ),
            ) and (conn and conn.proxy and not conn.has_connected_to_proxy):
                new_e = _wrap_proxy_error(new_e, conn.proxy.scheme)
            elif isinstance(new_e, OSError):
                new_e = ProtocolError("Connection aborted.", new_e)

            retries = retries.increment(
                method, url, error=new_e, _pool=self, _stacktrace=sys.exc_info()[2]
            )
            retries.sleep()

            # Keep track of the error for the retry warning.
            err = e

        finally:
            if not clean_exit:
                # We hit some kind of exception, handled or otherwise. We need
                # to throw the connection away unless explicitly told not to.
                # Close the connection, set the variable to None, and make sure
                # we put the None back in the pool to avoid leaking it.
                if conn:
                    conn.close()
                    conn = None
                release_this_conn = True
            elif conn and conn.is_multiplexed is True:
                # multiplexing allows us to issue more requests.
                release_this_conn = True

            if release_this_conn is True and conn is not None:
                # Put the connection back to be reused. If the connection is
                # expired then it will be None, which will get replaced with a
                # fresh connection during _get_conn.
                self._put_conn(conn)
                if (
                    clean_exit
                    and isinstance(response, ResponsePromise)
                    and self.pool is not None
                ):
                    self.pool.memorize(response, conn)
            elif release_this_conn is True and self.pool is not None:
                self.pool.kill_cursor()

        if not conn:
            # Try again
            log.warning(
                "Retrying (%r) after connection broken by '%r': %s", retries, err, url
            )
            return self.urlopen(  # type: ignore[no-any-return,call-overload,misc]
                method,
                url,
                body,
                headers,
                retries,
                redirect,
                assert_same_host,
                timeout=timeout,
                pool_timeout=pool_timeout,
                release_conn=release_conn,
                chunked=chunked,
                body_pos=body_pos,
                preload_content=preload_content,
                decode_content=decode_content,
                multiplexed=multiplexed,
                **response_kw,
            )

        if multiplexed:
            assert isinstance(response, ResponsePromise)
            return response  # actually a response promise!

        assert isinstance(response, HTTPResponse)

        if redirect and response.get_redirect_location():
            # Handle redirect?
            redirect_location = response.get_redirect_location()

            if response.status == 303:
                method = "GET"
                body = None
                headers = HTTPHeaderDict(headers)

                for should_be_removed_header in NOT_FORWARDABLE_HEADERS:
                    headers.discard(should_be_removed_header)

            try:
                retries = retries.increment(method, url, response=response, _pool=self)
            except MaxRetryError:
                if retries.raise_on_redirect:
                    response.drain_conn()
                    raise
                return response

            response.drain_conn()
            retries.sleep_for_retry(response)
            log.debug("Redirecting %s -> %s", url, redirect_location)
            return self.urlopen(  # type: ignore[call-overload,no-any-return,misc]
                method,
                redirect_location,
                body=body,
                headers=headers,
                retries=retries,
                redirect=redirect,
                assert_same_host=assert_same_host,
                timeout=timeout,
                pool_timeout=pool_timeout,
                release_conn=release_conn,
                chunked=chunked,
                body_pos=body_pos,
                preload_content=preload_content,
                decode_content=decode_content,
                multiplexed=False,
                **response_kw,
            )

        # Check if we should retry the HTTP response.
        has_retry_after = bool(response.headers.get("Retry-After"))
        if retries.is_retry(method, response.status, has_retry_after):
            try:
                retries = retries.increment(method, url, response=response, _pool=self)
            except MaxRetryError:
                if retries.raise_on_status:
                    response.drain_conn()
                    raise
                return response

            response.drain_conn()
            retries.sleep(response)
            log.debug("Retry: %s", url)
            return self.urlopen(
                method,
                url,
                body,
                headers,
                retries=retries,
                redirect=redirect,
                assert_same_host=assert_same_host,
                timeout=timeout,
                pool_timeout=pool_timeout,
                release_conn=release_conn,
                chunked=chunked,
                body_pos=body_pos,
                preload_content=preload_content,
                decode_content=decode_content,
                multiplexed=False,
                **response_kw,
            )

        return response


class HTTPSConnectionPool(HTTPConnectionPool):
    """
    Same as :class:`.HTTPConnectionPool`, but HTTPS.

    :class:`.HTTPSConnection` uses one of ``assert_fingerprint``,
    ``assert_hostname`` and ``host`` in this order to verify connections.
    If ``assert_hostname`` is False, no verification is done.

    The ``key_file``, ``cert_file``, ``cert_reqs``, ``ca_certs``,
    ``ca_cert_dir``, ``ssl_version``, ``key_password`` are only used if :mod:`ssl`
    is available and are fed into :meth:`urllib3.util.ssl_wrap_socket` to upgrade
    the connection socket into an SSL socket.
    """

    scheme = "https"
    ConnectionCls: type[HTTPSConnection] = HTTPSConnection

    def __init__(
        self,
        host: str,
        port: int | None = None,
        timeout: _TYPE_TIMEOUT | None = _DEFAULT_TIMEOUT,
        maxsize: int = 1,
        block: bool = False,
        headers: typing.Mapping[str, str] | None = None,
        retries: Retry | bool | int | None = None,
        _proxy: Url | None = None,
        _proxy_headers: typing.Mapping[str, str] | None = None,
        key_file: str | None = None,
        cert_file: str | None = None,
        cert_reqs: int | str | None = None,
        key_password: str | None = None,
        ca_certs: str | None = None,
        ssl_version: int | str | None = None,
        ssl_minimum_version: ssl.TLSVersion | None = None,
        ssl_maximum_version: ssl.TLSVersion | None = None,
        assert_hostname: str | Literal[False] | None = None,
        assert_fingerprint: str | None = None,
        ca_cert_dir: str | None = None,
        ca_cert_data: None | str | bytes = None,
        cert_data: str | bytes | None = None,
        key_data: str | bytes | None = None,
        **conn_kw: typing.Any,
    ) -> None:
        super().__init__(
            host,
            port,
            timeout,
            maxsize,
            block,
            headers,
            retries,
            _proxy,
            _proxy_headers,
            **conn_kw,
        )

        self.key_file = key_file
        self.cert_file = cert_file
        self.cert_reqs = cert_reqs
        self.key_password = key_password
        self.ca_certs = ca_certs
        self.ca_cert_dir = ca_cert_dir
        self.ca_cert_data = ca_cert_data
        self.cert_data = cert_data
        self.key_data = key_data
        self.ssl_version = ssl_version
        self.ssl_minimum_version = ssl_minimum_version
        self.ssl_maximum_version = ssl_maximum_version
        self.assert_hostname = assert_hostname
        self.assert_fingerprint = assert_fingerprint

    def _prepare_proxy(self, conn: HTTPSConnection) -> None:  # type: ignore[override]
        """Establishes a tunnel connection through HTTP CONNECT."""
        if self.proxy and self.proxy.scheme == "https":
            tunnel_scheme = "https"
        else:
            tunnel_scheme = "http"

        conn.set_tunnel(
            scheme=tunnel_scheme,
            host=self._tunnel_host,
            port=self.port,
            headers=self.proxy_headers,
        )
        conn.connect()

    def _new_conn(self, *, heb_timeout: Timeout | None = None) -> HTTPSConnection:
        """
        Return a fresh :class:`urllib3.connection.HTTPConnection`.
        """
        if self.pool is None:
            raise ClosedPoolError(self, "Pool is closed")
        self.num_connections += 1
        log.debug(
            "Starting new HTTPS connection (%d): %s:%s",
            self.num_connections,
            self.host,
            self.port or "443",
        )

        if not self.ConnectionCls or self.ConnectionCls is DummyConnection:  # type: ignore[comparison-overlap]
            raise ImportError(
                "Can't connect to HTTPS URL because the SSL module is not available."
            )

        actual_host: str = self.host
        actual_port = self.port
        if self.proxy is not None and self.proxy.host is not None:
            actual_host = self.proxy.host
            actual_port = self.proxy.port

        conn = None

        if self.happy_eyeballs:
            log.debug(
                "Attempting Happy-Eyeball %s:%s",
                self.host,
                self.port or "443",
            )

            dt_pre_resolve = datetime.now(tz=timezone.utc)
            ip_addresses = self._resolver.getaddrinfo(
                actual_host,
                actual_port,
                socket.AF_UNSPEC
                if "socket_family" not in self.conn_kw
                else self.conn_kw["socket_family"],
                socket.SOCK_STREAM,
                quic_upgrade_via_dns_rr=True,
            )
            delta_post_resolve = datetime.now(tz=timezone.utc) - dt_pre_resolve

            target_pqc = {}

            if (
                "preemptive_quic_cache" in self.conn_kw
                and self.conn_kw["preemptive_quic_cache"] is not None
            ):
                target_pqc = self.conn_kw["preemptive_quic_cache"]

            if any(_[1] == socket.SOCK_DGRAM for _ in ip_addresses):
                if (self.host, self.port) not in target_pqc:
                    target_pqc[(self.host, self.port)] = (self.host, self.port)

            if len(ip_addresses) > 1:
                ipv6_addresses = []
                ipv4_addresses = []

                for ip_address in ip_addresses:
                    if ip_address[0] == socket.AF_INET6:
                        ipv6_addresses.append(ip_address)
                    else:
                        ipv4_addresses.append(ip_address)

                if ipv4_addresses and ipv6_addresses:
                    log.debug(
                        "Happy-Eyeball Dual-Stack %s:%s",
                        self.host,
                        self.port or "443",
                    )

                    intermediary_addresses = []
                    for ipv6_entry, ipv4_entry in zip_longest(
                        ipv6_addresses, ipv4_addresses
                    ):
                        if ipv6_entry:
                            intermediary_addresses.append(ipv6_entry)
                        if ipv4_entry:
                            intermediary_addresses.append(ipv4_entry)
                    ip_addresses = intermediary_addresses
                else:
                    log.debug(
                        "Happy-Eyeball Single-Stack %s:%s",
                        self.host,
                        self.port or "443",
                    )

                challengers = []
                max_task = (
                    4 if isinstance(self.happy_eyeballs, bool) else self.happy_eyeballs
                )

                if heb_timeout is None:
                    heb_timeout = self.timeout

                override_timeout = (
                    heb_timeout.connect_timeout
                    if heb_timeout.connect_timeout is not None
                    and isinstance(heb_timeout.connect_timeout, (float, int))
                    else 0.4
                )

                for ip_address in ip_addresses[:max_task]:
                    conn_kw = self.conn_kw.copy()
                    target_solo_addr = (
                        f"[{ip_address[-1][0]}]"
                        if ip_address[0] == socket.AF_INET6
                        else ip_address[-1][0]
                    )
                    conn_kw["resolver"] = ResolverDescription.from_url(
                        f"in-memory://default?hosts={self.host}:{target_solo_addr}"
                    ).new()
                    conn_kw["socket_family"] = ip_address[0]
                    conn_kw["preemptive_quic_cache"] = target_pqc

                    challengers.append(
                        self.ConnectionCls(
                            host=actual_host,
                            port=actual_port,
                            timeout=override_timeout,
                            cert_file=self.cert_file,
                            key_file=self.key_file,
                            key_password=self.key_password,
                            cert_reqs=self.cert_reqs,
                            ca_certs=self.ca_certs,
                            ca_cert_dir=self.ca_cert_dir,
                            ca_cert_data=self.ca_cert_data,
                            assert_hostname=self.assert_hostname,
                            assert_fingerprint=self.assert_fingerprint,
                            ssl_version=self.ssl_version,
                            ssl_minimum_version=self.ssl_minimum_version,
                            ssl_maximum_version=self.ssl_maximum_version,
                            cert_data=self.cert_data,
                            key_data=self.key_data,
                            **conn_kw,
                        )
                    )

                event = threading.Event()
                winning_task: Future[None] | None = None
                completed_count: int = 0

                def _happy_eyeballs_completed(t: Future[None]) -> None:
                    nonlocal winning_task, event, completed_count

                    if winning_task is None and t.exception() is None:
                        winning_task = t
                        event.set()
                        return

                    completed_count += 1

                    if completed_count >= len(challengers):
                        event.set()

                tpe = ThreadPoolExecutor(max_workers=max_task)

                tasks: list[Future[None]] = []

                for challenger in challengers:
                    task = tpe.submit(challenger.connect)
                    task.add_done_callback(_happy_eyeballs_completed)

                    tasks.append(task)

                event.wait()

                for task in tasks:
                    if task == winning_task:
                        continue

                    if task.running():
                        task.cancel()
                    else:
                        challengers[tasks.index(task)].close()

                if winning_task is None:
                    within_delay_msg: str = (
                        f" within {override_timeout}s" if override_timeout else ""
                    )
                    raise NewConnectionError(
                        challengers[0],
                        f"Failed to establish a new connection: No suitable address to connect to using Happy Eyeballs algorithm for {actual_host}:{actual_port}{within_delay_msg}",
                    ) from tasks[0].exception()

                conn = challengers[tasks.index(winning_task)]

                # we have to replace the resolution latency metric
                if conn.conn_info:
                    conn.conn_info.resolution_latency = delta_post_resolve

                tpe.shutdown(wait=False)
            else:
                log.debug(
                    "Happy-Eyeball Ineligible %s:%s",
                    self.host,
                    self.port or "443",
                )

        if conn is None:
            conn = self.ConnectionCls(
                host=actual_host,
                port=actual_port,
                timeout=self.timeout.connect_timeout,
                cert_file=self.cert_file,
                key_file=self.key_file,
                key_password=self.key_password,
                cert_reqs=self.cert_reqs,
                ca_certs=self.ca_certs,
                ca_cert_dir=self.ca_cert_dir,
                ca_cert_data=self.ca_cert_data,
                assert_hostname=self.assert_hostname,
                assert_fingerprint=self.assert_fingerprint,
                ssl_version=self.ssl_version,
                ssl_minimum_version=self.ssl_minimum_version,
                ssl_maximum_version=self.ssl_maximum_version,
                cert_data=self.cert_data,
                key_data=self.key_data,
                **self.conn_kw,
            )

        self.pool.put(conn, immediately_unavailable=True)
        return conn

    def _validate_conn(self, conn: HTTPConnection) -> None:
        """
        Called right before a request is made, after the socket is created.
        """
        super()._validate_conn(conn)

        # Force connect early to allow us to validate the connection.
        if conn.is_closed:
            conn.connect()

        if not conn.is_verified:
            warnings.warn(
                (
                    f"Unverified HTTPS request is being made to host '{conn.host}'. "
                    "Adding certificate verification is strongly advised. See: "
                    "https://urllib3future.readthedocs.io/en/latest/advanced-usage.html"
                    "#tls-warnings"
                ),
                InsecureRequestWarning,
            )


def connection_from_url(url: str, **kw: typing.Any) -> HTTPConnectionPool:
    """
    Given a url, return an :class:`.ConnectionPool` instance of its host.

    This is a shortcut for not having to parse out the scheme, host, and port
    of the url before creating an :class:`.ConnectionPool` instance.

    :param url:
        Absolute URL string that must include the scheme. Port is optional.

    :param \\**kw:
        Passes additional parameters to the constructor of the appropriate
        :class:`.ConnectionPool`. Useful for specifying things like
        timeout, maxsize, headers, etc.

    Example::

        >>> conn = connection_from_url('http://google.com/')
        >>> r = conn.request('GET', '/')
    """
    scheme, _, host, port, *_ = parse_url(url)
    scheme = scheme or "http"
    port = port or port_by_scheme.get(scheme, 80)
    if scheme == "https":
        return HTTPSConnectionPool(host, port=port, **kw)  # type: ignore[arg-type]
    else:
        return HTTPConnectionPool(host, port=port, **kw)  # type: ignore[arg-type]


@typing.overload
def _normalize_host(host: None, scheme: str | None) -> None:
    ...


@typing.overload
def _normalize_host(host: str, scheme: str | None) -> str:
    ...


def _normalize_host(host: str | None, scheme: str | None) -> str | None:
    """
    Normalize hosts for comparisons and use with sockets.
    """

    host = normalize_host(host, scheme)

    # httplib doesn't like it when we include brackets in IPv6 addresses
    # Specifically, if we include brackets but also pass the port then
    # httplib crazily doubles up the square brackets on the Host header.
    # Instead, we need to make sure we never pass ``None`` as the port.
    # However, for backward compatibility reasons we can't actually
    # *assert* that.  See http://bugs.python.org/issue28539
    if host and host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host


def _url_from_pool(
    pool: HTTPConnectionPool | HTTPSConnectionPool, path: str | None = None
) -> str:
    """Returns the URL from a given connection pool. This is mainly used for testing and logging."""
    return Url(scheme=pool.scheme, host=pool.host, port=pool.port, path=path).url
