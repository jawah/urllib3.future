from __future__ import annotations

import socket
import struct
import threading
import typing
from abc import ABCMeta, abstractmethod
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from enum import Enum
from random import randint

from ..._typing import _TYPE_SOCKET_OPTIONS, _TYPE_TIMEOUT_INTERNAL
from ...exceptions import LocationParseError
from ...util.connection import _set_socket_options, allowed_gai_family
from ...util.ssl_match_hostname import CertificateError, match_hostname
from ...util.timeout import _DEFAULT_TIMEOUT
from .utils import inet4_ntoa, inet6_ntoa


class ProtocolResolver(str, Enum):
    """
    At urllib3.future we aim to propose a wide range of DNS-protocols.
    The most used techniques are available.
    """

    #: Ask the OS native DNS layer
    SYSTEM = "system"
    #: DNS over HTTPS
    DOH = "doh"
    #: DNS over QUIC
    DOQ = "doq"
    #: DNS over TLS
    DOT = "dot"
    #: DNS over UDP (insecure)
    DOU = "dou"
    #: Manual (e.g. hosts)
    MANUAL = "in-memory"
    #: Void (e.g. purposely disable resolution)
    NULL = "null"
    #: Custom (e.g. your own implementation, use this when it does not suit any of the protocols specified)
    CUSTOM = "custom"


class BaseResolver(metaclass=ABCMeta):
    protocol: typing.ClassVar[ProtocolResolver]
    specifier: typing.ClassVar[str | None] = None

    implementation: typing.ClassVar[str]

    def __init__(
        self,
        server: str | None,
        port: int | None = None,
        *patterns: str,
        **kwargs: typing.Any,
    ) -> None:
        self._server = server
        self._port = port
        self._host_patterns: tuple[str, ...] = patterns
        self._lock = threading.Lock()
        self._kwargs = kwargs

        if not self._host_patterns and "patterns" in kwargs:
            self._host_patterns = kwargs["patterns"]

    def recycle(self) -> BaseResolver:
        if self.is_available():
            raise RuntimeError("Attempting to recycle a Resolver that was not closed")

        args = list(self.__class__.__init__.__code__.co_varnames)
        args.remove("self")

        kwargs_cpy = deepcopy(self._kwargs)

        if self._server:
            kwargs_cpy["server"] = self._server
        if self._port:
            kwargs_cpy["port"] = self._port

        if "patterns" in args and "kwargs" in args:
            return self.__class__(*self._host_patterns, **kwargs_cpy)  # type: ignore[arg-type]
        elif "kwargs" in args:
            return self.__class__(**kwargs_cpy)

        return self.__class__()  # type: ignore[call-arg]

    @property
    def server(self) -> str | None:
        return self._server

    @property
    def port(self) -> int | None:
        return self._port

    def have_constraints(self) -> bool:
        return bool(self._host_patterns)

    def support(self, hostname: str | bytes | None) -> bool | None:
        """
        Determine if given hostname is especially resolvable by given resolver.
        If this resolver does not have any constrained list of host, it returns None. Meaning
        it support any hostname for resolution.
        """
        if not self._host_patterns:
            return None
        if hostname is None:
            hostname = "localhost"
        if isinstance(hostname, bytes):
            hostname = hostname.decode("ascii")
        try:
            match_hostname(
                {"subjectAltName": (tuple(("DNS", e) for e in self._host_patterns))},
                hostname,
            )
        except CertificateError:
            return False
        return True

    @abstractmethod
    def close(self) -> None:
        """Terminate the given resolver instance. This should render it unusable. Further inquiries should raise an exception."""
        raise NotImplementedError

    @abstractmethod
    def is_available(self) -> bool:
        """Determine if Resolver can receive inquiries."""
        raise NotImplementedError

    @abstractmethod
    def getaddrinfo(
        self,
        host: bytes | str | None,
        port: str | int | None,
        family: socket.AddressFamily,
        type: socket.SocketKind,
        proto: int = 0,
        flags: int = 0,
        *,
        quic_upgrade_via_dns_rr: bool = False,
    ) -> list[
        tuple[
            socket.AddressFamily,
            socket.SocketKind,
            int,
            str,
            tuple[str, int] | tuple[str, int, int, int],
        ]
    ]:
        """This method align itself on the standard library socket.getaddrinfo(). It must be implemented as-is on your Resolver."""
        raise NotImplementedError

    # This function is copied from socket.py in the Python 2.7 standard
    # library test suite. Added to its signature is only `socket_options`.
    # One additional modification is that we avoid binding to IPv6 servers
    # discovered in DNS if the system doesn't have IPv6 functionality.
    def create_connection(
        self,
        address: tuple[str, int],
        timeout: _TYPE_TIMEOUT_INTERNAL = _DEFAULT_TIMEOUT,
        source_address: tuple[str, int] | None = None,
        socket_options: _TYPE_SOCKET_OPTIONS | None = None,
        socket_kind: socket.SocketKind = socket.SOCK_STREAM,
        *,
        quic_upgrade_via_dns_rr: bool = False,
        timing_hook: typing.Callable[[tuple[timedelta, timedelta, datetime]], None]
        | None = None,
        default_socket_family: socket.AddressFamily = socket.AF_UNSPEC,
    ) -> socket.socket:
        """Connect to *address* and return the socket object.

        Convenience function.  Connect to *address* (a 2-tuple ``(host,
        port)``) and return the socket object.  Passing the optional
        *timeout* parameter will set the timeout on the socket instance
        before attempting to connect.  If no *timeout* is supplied, the
        global default timeout setting returned by :func:`socket.getdefaulttimeout`
        is used.  If *source_address* is set it must be a tuple of (host, port)
        for the socket to bind as a source address before making the connection.
        An host of '' or port 0 tells the OS to use the default.
        """

        host, port = address
        if host.startswith("["):
            host = host.strip("[]")
        err = None

        # Using the value from allowed_gai_family() in the context of getaddrinfo lets
        # us select whether to work with IPv4 DNS records, IPv6 records, or both.
        # The original create_connection function always returns all records.
        family = allowed_gai_family()

        if family != socket.AF_UNSPEC:
            default_socket_family = family

        try:
            host.encode("idna")
        except UnicodeError:
            raise LocationParseError(f"'{host}', label empty or too long") from None

        dt_pre_resolve = datetime.now(tz=timezone.utc)
        records = self.getaddrinfo(
            host,
            port,
            default_socket_family,
            socket_kind,
            quic_upgrade_via_dns_rr=quic_upgrade_via_dns_rr,
        )
        delta_post_resolve = datetime.now(tz=timezone.utc) - dt_pre_resolve

        dt_pre_established = datetime.now(tz=timezone.utc)
        for res in records:
            af, socktype, proto, canonname, sa = res
            sock = None
            try:
                sock = socket.socket(af, socktype, proto)

                # If provided, set socket level options before connecting.
                _set_socket_options(sock, socket_options)

                if timeout is not _DEFAULT_TIMEOUT:
                    sock.settimeout(timeout)
                if source_address:
                    sock.bind(source_address)
                sock.connect(sa)
                # Break explicitly a reference cycle
                err = None

                delta_post_established = (
                    datetime.now(tz=timezone.utc) - dt_pre_established
                )

                if timing_hook is not None:
                    timing_hook(
                        (
                            delta_post_resolve,
                            delta_post_established,
                            datetime.now(tz=timezone.utc),
                        )
                    )

                return sock
            except (OSError, OverflowError) as _:
                err = _
                if sock is not None:
                    sock.close()
                if isinstance(_, OverflowError):
                    break

        if err is not None:
            try:
                raise err
            finally:
                # Break explicitly a reference cycle
                err = None
        else:
            raise OSError("getaddrinfo returns an empty list")


class ManyResolver(BaseResolver):
    """
    Special resolver that use many child resolver. Priorities
    are based on given order (list of BaseResolver).
    """

    def __init__(self, *resolvers: BaseResolver) -> None:
        super().__init__(None, None)

        self._size = len(resolvers)

        self._unconstrained: list[BaseResolver] = [
            _ for _ in resolvers if not _.have_constraints()
        ]
        self._constrained: list[BaseResolver] = [
            _ for _ in resolvers if _.have_constraints()
        ]

        self._concurrent: int = 0
        self._terminated: bool = False

    def recycle(self) -> BaseResolver:
        resolvers = []

        for resolver in self._unconstrained + self._constrained:
            resolvers.append(resolver.recycle())

        return ManyResolver(*resolvers)

    def close(self) -> None:
        for resolver in self._unconstrained + self._constrained:
            resolver.close()

        self._terminated = True

    def is_available(self) -> bool:
        return not self._terminated

    def __resolvers(
        self, constrained: bool = False
    ) -> typing.Generator[BaseResolver, None, None]:
        resolvers = self._unconstrained if not constrained else self._constrained

        if not resolvers:
            return

        with self._lock:
            self._concurrent += 1

        try:
            resolver_count = len(resolvers)
            start_idx = (self._concurrent - 1) % resolver_count

            for idx in range(start_idx, resolver_count):
                if not resolvers[idx].is_available():
                    with self._lock:
                        resolvers[idx] = resolvers[idx].recycle()
                yield resolvers[idx]

            if start_idx > 0:
                for idx in range(0, start_idx):
                    if not resolvers[idx].is_available():
                        with self._lock:
                            resolvers[idx] = resolvers[idx].recycle()
                    yield resolvers[idx]
        finally:
            with self._lock:
                self._concurrent -= 1

    def getaddrinfo(
        self,
        host: bytes | str | None,
        port: str | int | None,
        family: socket.AddressFamily,
        type: socket.SocketKind,
        proto: int = 0,
        flags: int = 0,
        *,
        quic_upgrade_via_dns_rr: bool = False,
    ) -> list[
        tuple[
            socket.AddressFamily,
            socket.SocketKind,
            int,
            str,
            tuple[str, int] | tuple[str, int, int, int],
        ]
    ]:
        if isinstance(host, bytes):
            host = host.decode("ascii")
        if host is None:
            host = "localhost"

        tested_resolvers = []

        any_constrained_tried: bool = False

        for resolver in self.__resolvers(True):
            can_resolve = resolver.support(host)

            if can_resolve is True:
                any_constrained_tried = True

                try:
                    results = resolver.getaddrinfo(
                        host,
                        port,
                        family,
                        type,
                        proto,
                        flags,
                        quic_upgrade_via_dns_rr=quic_upgrade_via_dns_rr,
                    )

                    if results:
                        return results
                except socket.gaierror as exc:
                    if isinstance(exc.args[0], str) and (
                        "DNSSEC" in exc.args[0] or "DNSKEY" in exc.args[0]
                    ):
                        raise
                    continue
            elif can_resolve is False:
                tested_resolvers.append(resolver)

        if any_constrained_tried:
            raise socket.gaierror(
                f"Name or service not known: {host} using {self._size - len(self._unconstrained)} resolver(s)"
            )

        for resolver in self.__resolvers():
            try:
                results = resolver.getaddrinfo(
                    host,
                    port,
                    family,
                    type,
                    proto,
                    flags,
                    quic_upgrade_via_dns_rr=quic_upgrade_via_dns_rr,
                )

                if results:
                    return results
            except socket.gaierror as exc:
                if isinstance(exc.args[0], str) and (
                    "DNSSEC" in exc.args[0] or "DNSKEY" in exc.args[0]
                ):
                    raise
                continue

        raise socket.gaierror(
            f"Name or service not known: {host} using {self._size - len(self._constrained)} resolver(s)"
        )


class SupportedQueryType(int, Enum):
    """
    urllib3.future does not need anything else so far. let's be pragmatic.
    Each type is associated with its hex value as per the RFC.
    """

    A = 0x0001
    AAAA = 0x001C
    HTTPS = 0x0041


class DomainNameServerQuery:
    """
    Minimalist DNS query/message to ask for A, AAAA and HTTPS records.
    Only meant for urllib3.future use. Does not cover all of possible extent of use.
    """

    def __init__(
        self, host: str, query_type: SupportedQueryType, override_id: int | None = None
    ) -> None:
        self._id = struct.pack(
            "!H", randint(0x0000, 0xFFFF) if override_id is None else override_id
        )
        self._host = host
        self._query = query_type
        self._flags = struct.pack("!H", 0x0100)
        self._qd_count = struct.pack("!H", 1)

        self._cached: bytes | None = None

    @property
    def id(self) -> int:
        return struct.unpack("!H", self._id)[0]  # type: ignore[no-any-return]

    @property
    def raw_id(self) -> bytes:
        return self._id

    def __repr__(self) -> str:
        return f"<Query '{self._host}' IN {self._query.name}>"

    def __bytes__(self) -> bytes:
        if self._cached:
            return self._cached

        payload = b""

        payload += self._id
        payload += self._flags
        payload += self._qd_count
        payload += b"\x00\x00"
        payload += b"\x00\x00"
        payload += b"\x00\x00"

        for ext in self._host.split("."):
            payload += struct.pack("!B", len(ext))
            payload += ext.encode("ascii")

        payload += b"\x00"
        payload += struct.pack("!H", self._query.value)
        payload += struct.pack("!H", 0x0001)

        self._cached = payload

        return payload

    @staticmethod
    def bulk(host: str, *types: SupportedQueryType) -> list[DomainNameServerQuery]:
        queries = []

        for query_type in types:
            queries.append(DomainNameServerQuery(host, query_type=query_type))

        return queries


#: Most common status code, not exhaustive at all.
COMMON_RCODE_LABEL: dict[int, str] = {
    0: "No Error",
    1: "Format Error",
    2: "Server Failure",
    3: "Non-Existent Domain",
    5: "Query Refused",
    9: "Not Authorized",
}


class DomainNameServerParseException(Exception):
    ...


class DomainNameServerReturn:
    """
    Minimalist DNS response parser. Allow to quickly extract key-data out of it.
    Meant for A, AAAA and HTTPS records. Basically only what we need.
    """

    def __init__(self, payload: bytes) -> None:
        try:
            up = struct.unpack("!HHHHHH", payload[:12])

            self._id = up[0]
            self._flags = up[1]
            self._qd_count = up[2]
            self._an_count = up[3]

            self._rcode = int(f"0x{hex(payload[3])[-1]}", 16)

            self._hostname: str = ""

            idx = 12

            while True:
                c = payload[idx]

                if c == 0:
                    idx += 1
                    break

                self._hostname += payload[idx + 1 : idx + 1 + c].decode("ascii") + "."

                idx += c + 1

            self._records: list[tuple[SupportedQueryType, int, str]] = []

            if self._an_count:
                idx += 4

                while idx < len(payload):
                    up = struct.unpack("!HHHI", payload[idx : idx + 10])
                    entry_size = struct.unpack("!H", payload[idx + 10 : idx + 12])[0]

                    data = payload[idx + 12 : idx + 12 + entry_size]

                    if len(data) == 4:
                        decoded_data = inet4_ntoa(data)
                    elif len(data) == 16:
                        decoded_data = inet6_ntoa(data)
                    else:
                        alpn = []
                        if b"h2" in data:
                            alpn.append("h2")
                        if b"h3" in data:
                            alpn.append("h3")
                        decoded_data = f"alpn={','.join(alpn)}"

                    try:
                        self._records.append(
                            (SupportedQueryType(up[1]), up[-1], decoded_data)
                        )
                    except ValueError:
                        pass

                    idx += 12 + entry_size
        except (struct.error, IndexError, ValueError, UnicodeDecodeError) as e:
            raise DomainNameServerParseException(
                "A protocol error occurred while parsing the DNS response payload: "
                f"{str(e)}"
            ) from e

    @property
    def id(self) -> int:
        return self._id  # type: ignore[no-any-return]

    @property
    def hostname(self) -> str:
        return self._hostname

    @property
    def records(self) -> list[tuple[SupportedQueryType, int, str]]:
        return self._records

    @property
    def is_found(self) -> bool:
        return bool(self._records)

    @property
    def rcode(self) -> int:
        return self._rcode

    @property
    def is_ok(self) -> bool:
        return self._rcode == 0

    def __repr__(self) -> str:
        if self.is_ok:
            return f"<Records '{self.hostname}' {self._records}>"
        return f"<DNS Error '{self.hostname}' with Status {self.rcode} ({COMMON_RCODE_LABEL[self.rcode] if self.rcode in COMMON_RCODE_LABEL else 'Unknown'})>"
