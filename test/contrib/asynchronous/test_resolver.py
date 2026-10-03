from __future__ import annotations

import asyncio
import socket
import struct
from base64 import b64encode
from test import requires_network
from test.conftest import DNSHTTPSServer, DNSTLSServer, DNSUDPServer
from unittest.mock import MagicMock, Mock

import pytest

from urllib3 import ConnectionInfo, HttpVersion
from urllib3.contrib.anytls import BACKEND, ssl
from urllib3.contrib.resolver import ProtocolResolver
from urllib3.contrib.resolver._async import (
    AsyncBaseResolver,
    AsyncManyResolver,
    AsyncResolverDescription,
)
from urllib3.contrib.resolver._async.doh import HTTPSResolver

from urllib3.contrib.resolver._async.factories import AsyncResolverFactory


_MISSING_QUIC_SENTINEL = object()

try:
    from urllib3.contrib.resolver._async.doq._qh3 import QUICResolver
except ImportError:
    QUICResolver = _MISSING_QUIC_SENTINEL  # type: ignore

from urllib3.contrib.resolver._async.dot import TLSResolver  # noqa: E402
from urllib3.contrib.resolver._async.dou import PlainResolver  # noqa: E402
from urllib3.contrib.resolver._async.in_memory import InMemoryResolver  # noqa: E402
from urllib3.contrib.resolver._async.null import NullResolver  # noqa: E402
from urllib3.contrib.resolver._async.system import SystemResolver  # noqa: E402
from urllib3.exceptions import InsecureRequestWarning  # noqa: E402


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dns_tls_server", ["coalesced", "large", "malformed"], indirect=True
)
async def test_dot_framing_and_reuse(dns_tls_server: DNSTLSServer) -> None:
    server = dns_tls_server
    resolver = TLSResolver(
        *server.address,
        timeout=5,
        ca_certs=server.ca_certs,
        source_address="127.0.0.1:0",
    )
    try:
        expected = {
            (socket.AF_INET, f"192.0.2.{i}") for i in range(1, server.answer_count + 1)
        }
        expected.update(
            (socket.AF_INET6, f"2001:db8::{i:x}")
            for i in range(1, server.answer_count + 1)
        )
        for host, port in (("first.example", 443), ("second.example", 8443)):
            results = await resolver.getaddrinfo(
                host, port, socket.AF_UNSPEC, socket.SOCK_STREAM
            )
            assert {(r[0], r[4][0]) for r in results} == expected
            assert len(results) == len(expected)
            assert all(
                r[1:4] == (socket.SOCK_STREAM, 6, "") and r[4][1] == port
                for r in results
            )
            assert resolver.is_available()
        assert sorted(struct.unpack("!H", q[-4:-2])[0] for q in server.requests) == [
            1,
            1,
            28,
            28,
            65,
            65,
        ]
    finally:
        await resolver.close()
    assert not resolver.is_available()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dns_tls_server", ["empty", "partial-prefix", "partial-body"], indirect=True
)
@pytest.mark.xfail(
    BACKEND == "utls",
    reason="utls SSLObject.read raises on close_notify; see UTLS_SSL_OBJECT_CLEAN_EOF.md",
    raises=AssertionError,
    strict=True,
)
async def test_dot_eof_closes_resolver(dns_tls_server: DNSTLSServer) -> None:
    resolver = TLSResolver(
        *dns_tls_server.address, timeout=5, ca_certs=dns_tls_server.ca_certs
    )
    try:
        with pytest.raises(
            socket.gaierror,
            match="DNS server closed the connection before sending a complete response",
        ):
            await resolver.getaddrinfo(
                "first.example", 443, socket.AF_UNSPEC, socket.SOCK_STREAM
            )
        assert not resolver.is_available()
    finally:
        await resolver.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("dns_tls_server", ["timeout"], indirect=True)
async def test_dot_response_timeout(dns_tls_server: DNSTLSServer) -> None:
    resolver = TLSResolver(
        *dns_tls_server.address, timeout=5, ca_certs=dns_tls_server.ca_certs
    )
    try:
        with pytest.raises(
            socket.gaierror, match="while waiting for name resolution"
        ) as exc:
            await resolver.getaddrinfo(
                "first.example", 443, socket.AF_UNSPEC, socket.SOCK_STREAM
            )
        assert isinstance(exc.value.__cause__, (socket.timeout, TimeoutError))
    finally:
        await resolver.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("dns_tls_server", ["handshake"], indirect=True)
async def test_dot_rejects_wrong_tls_hostname(dns_tls_server: DNSTLSServer) -> None:
    resolver = TLSResolver(
        *dns_tls_server.address,
        timeout=5,
        ca_certs=dns_tls_server.ca_certs,
        server_hostname="wrong.example",
    )
    try:
        with pytest.raises(ssl.SSLError):
            await resolver.getaddrinfo(
                "first.example", 443, socket.AF_UNSPEC, socket.SOCK_STREAM
            )
        assert not dns_tls_server.requests
    finally:
        await resolver.close()


@pytest.mark.asyncio
async def test_create_connection_reports_ech_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib3.contrib.resolver._async import protocols

    ech_config = b"ech-config"
    resolver = SystemResolver()

    async def getaddrinfo(
        *args: object, **kwargs: object
    ) -> list[
        tuple[
            socket.AddressFamily,
            socket.SocketKind,
            int,
            bytes,
            tuple[str, int],
        ]
    ]:
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                ech_config,
                ("127.0.0.1", 443),
            )
        ]

    async def connect(address: tuple[str, int]) -> None:
        pass

    sock = MagicMock()
    sock.connect = connect
    monkeypatch.setattr(resolver, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(protocols, "AsyncSocket", Mock(return_value=sock))
    ech_config_hook = Mock()

    assert (
        await resolver.create_connection(
            ("example.com", 443), ech_config_hook=ech_config_hook
        )
        is sock
    )
    ech_config_hook.assert_called_once_with(ech_config)


_DOQ_PARAM = pytest.param(
    "doq://dns.adguard-dns.com/?timeout=5&cert_reqs=0",
    marks=[
        pytest.mark.skipif(
            QUICResolver is _MISSING_QUIC_SENTINEL,
            reason="Test requires qh3 installed",
        ),
    ],
)


@pytest.mark.parametrize(
    "hostname, expect_error",
    [
        ("abc.com", True),
        ("1.1.1.1", False),
        ("8.8.8.com", True),
        ("cloudflare.com", True),
    ],
)
@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["null://default", "null://unused.invalid:853"])
async def test_null_resolver(hostname: str, expect_error: bool, url: str) -> None:
    null_resolver = AsyncResolverDescription.from_url(url).new()

    if expect_error:
        with pytest.raises(socket.gaierror):
            await null_resolver.getaddrinfo(
                hostname,
                80,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
    else:
        res = await null_resolver.getaddrinfo(
            hostname,
            80,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )

        assert len(res)


@pytest.mark.parametrize(
    "url, expected_resolver_class",
    [
        ("dou://1.1.1.1", PlainResolver),
        ("dox://ooooo.com", None),
        ("doh://dns.google/resolve", HTTPSResolver),
        pytest.param(
            "doq://dns.adguard-dns.com/?timeout=5&cert_reqs=0",
            QUICResolver,
            marks=[
                pytest.mark.skipif(
                    QUICResolver is _MISSING_QUIC_SENTINEL,
                    reason="Test requires qh3 installed",
                ),
            ],
        ),
        ("dns://dns.adguard-dns.com", None),
        ("null://default", NullResolver),
        ("default://null", None),
        ("system://default", SystemResolver),
        ("system://noop", SystemResolver),
        ("in-memory://noop", InMemoryResolver),
        ("in-memory://default", InMemoryResolver),
        ("DoU://1.1.1.1", PlainResolver),
        ("DOH+GOOGLE://default", HTTPSResolver),
        ("doT://1.1.1.1", TLSResolver),
        ("dot://1.1.1.1/?implementation=nonexistent", None),
        ("system://", SystemResolver),
        ("dot://", None),
        pytest.param(
            "doq://dns.adguard-dns.com/?implementation=qh3&timeout=1&cert_reqs=0",
            QUICResolver,
            marks=[
                pytest.mark.skipif(
                    QUICResolver is _MISSING_QUIC_SENTINEL,
                    reason="Test requires qh3 installed",
                ),
            ],
        ),
    ],
)
@pytest.mark.asyncio
async def test_url_resolver(
    url: str, expected_resolver_class: type[AsyncBaseResolver] | None
) -> None:
    if expected_resolver_class is None:
        with pytest.raises(
            (
                NotImplementedError,
                ValueError,
                TypeError,
            )
        ):
            AsyncResolverDescription.from_url(url).new()
        return

    resolver = AsyncResolverDescription.from_url(url).new()

    assert isinstance(resolver, expected_resolver_class)
    await resolver.close()


@requires_network()
@pytest.mark.parametrize(
    "dns_url",
    [
        "dou://1.1.1.1",
        "dou://one.one.one.one",
        "dou://dns.google",
        "doh://cloudflare-dns.com/dns-query",
        "doh://dns.google",
        "system://default",
        "dot://dns.google",
        "dot://one.one.one.one",
        _DOQ_PARAM,
        "doh+google://",
        "doh+cloudflare://default",
    ],
)
@pytest.mark.asyncio
async def test_1_1_1_1_ipv4_resolution_across_protocols(dns_url: str) -> None:
    resolver = AsyncResolverDescription.from_url(dns_url).new()

    res = await resolver.getaddrinfo(
        "one.one.one.one",
        443,
        socket.AF_INET,
        socket.SOCK_STREAM,
        quic_upgrade_via_dns_rr=False,
    )

    assert any([_[-1][0] in ["1.1.1.1", "1.0.0.1"] for _ in res])
    await resolver.close()


@requires_network()
@pytest.mark.parametrize(
    "dns_url",
    [
        "dou://1.1.1.1",
        "dou://one.one.one.one",
        "dou://dns.google",
        # "doh://cloudflare-dns.com/dns-query",
        "doh://dns.google",
        "dot://dns.google",
        "dot://one.one.one.one",
        _DOQ_PARAM,
    ],
)
@pytest.mark.parametrize(
    "hostname, expected_failure",
    [
        ("brokendnssec.net", True),
        ("one.one.one.one", False),
        ("google.com", False),
    ],
)
@pytest.mark.asyncio
async def test_dnssec_exception(
    dns_url: str, hostname: str, expected_failure: bool
) -> None:
    resolver = AsyncResolverDescription.from_url(dns_url).new()

    if expected_failure:
        with pytest.raises(socket.gaierror, match="DNSSEC|DNSKEY"):
            await resolver.getaddrinfo(
                hostname,
                443,
                socket.AF_INET,
                socket.SOCK_STREAM,
                quic_upgrade_via_dns_rr=False,
            )
        await resolver.close()
        return

    res = await resolver.getaddrinfo(
        hostname,
        443,
        socket.AF_INET,
        socket.SOCK_STREAM,
        quic_upgrade_via_dns_rr=False,
    )

    assert len(res)
    await resolver.close()


@pytest.mark.parametrize(
    "hostname",
    [
        ("a" * 253) + ".com",
        ("b" * 64) + "aa.fr",
    ],
)
@pytest.mark.parametrize(
    "dns_url",
    [
        "system://",
        "dou://localhost",
    ],
)
@pytest.mark.asyncio
async def test_hostname_too_long(dns_url: str, hostname: str) -> None:
    resolver = AsyncResolverDescription.from_url(dns_url).new()

    with pytest.raises(
        UnicodeError, match="exceed 63 characters|exceed 253 characters|too long"
    ):
        await resolver.getaddrinfo(
            hostname,
            80,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )

    await resolver.close()


@pytest.mark.asyncio
async def test_many_resolver_host_constraint_distribution() -> None:
    resolvers = [
        AsyncResolverDescription.from_url("system://default?hosts=localhost").new(),
        AsyncResolverDescription.from_url("dou://127.0.0.1").new(),
        AsyncResolverDescription.from_url("in-memory://").new(),
    ]

    assert resolvers[0].have_constraints()
    assert not resolvers[1].have_constraints()
    assert resolvers[2].have_constraints()

    imr = resolvers[-1]

    imr.register("notlocalhost", "127.5.5.1")  # type: ignore[attr-defined]
    imr.register("c.localhost.eu", "127.8.8.1")  # type: ignore[attr-defined]
    imr.register("c.localhost.eu", "::1")  # type: ignore[attr-defined]

    resolver = AsyncManyResolver(*resolvers)

    res = await resolver.getaddrinfo(
        "localhost",
        80,
        socket.AF_UNSPEC,
        socket.SOCK_STREAM,
    )

    assert len(res)
    assert any(_[-1][0] == "127.0.0.1" for _ in res)

    res = await resolver.getaddrinfo(
        "notlocalhost",
        80,
        socket.AF_UNSPEC,
        socket.SOCK_STREAM,
    )

    assert len(res) == 1
    assert any(_[-1][0] == "127.5.5.1" for _ in res)

    res = await resolver.getaddrinfo(
        "c.localhost.eu",
        80,
        socket.AF_UNSPEC,
        socket.SOCK_STREAM,
    )

    assert len(res) == 2
    assert any(_[-1][0] == "127.8.8.1" for _ in res)
    assert any(_[-1][0] == "::1" for _ in res)

    await resolver.close()


@requires_network()
@pytest.mark.parametrize(
    "dns_url",
    [
        "doh+google://default/?timeout=1",
        "doh+cloudflare://",
        _DOQ_PARAM,
        "dot://one.one.one.one",
        "dou://one.one.one.one",
    ],
)
@pytest.mark.asyncio
async def test_short_endurance_sprint(dns_url: str) -> None:
    resolver = AsyncResolverDescription.from_url(dns_url).new()

    for host in [
        "www.google.com",
        "www.google.fr",
        "www.cloudflare.com",
        "youtube.com",
    ]:
        for addr_type in [socket.AF_UNSPEC, socket.AF_INET, socket.AF_INET6]:
            res = await resolver.getaddrinfo(
                host,
                443,
                addr_type,
                socket.SOCK_STREAM,
            )

            assert len(res)

            if addr_type == socket.AF_UNSPEC:
                assert any(_[0] == socket.AF_INET6 for _ in res)
                assert any(_[0] == socket.AF_INET for _ in res)
            elif addr_type == socket.AF_INET:
                assert all(_[0] == socket.AF_INET for _ in res)
            elif addr_type == socket.AF_INET6:
                assert all(_[0] == socket.AF_INET6 for _ in res)

    await resolver.close()


@requires_network()
@pytest.mark.parametrize(
    "dns_url",
    [
        "doh+google://default?rfc8484=true&timeout=5",
        "doh+google://default?rfc8484=true&disabled_svn=h11,h3&timeout=5",
        "doh+cloudflare://default?rfc8484=true&timeout=5",
        "doh://dns.adguard-dns.com/dns-query?rfc8484=true&timeout=5",
        "doh+adguard://",
    ],
)
@pytest.mark.asyncio
async def test_doh_rfc8484(dns_url: str) -> None:
    resolver = AsyncResolverDescription.from_url(dns_url).new()

    for host in [
        "www.google.com",
        "www.google.fr",
        "www.cloudflare.com",
        "youtube.com",
    ]:
        for addr_type in [socket.AF_UNSPEC, socket.AF_INET, socket.AF_INET6]:
            res = await resolver.getaddrinfo(
                host,
                443,
                addr_type,
                socket.SOCK_STREAM,
            )

            assert len(res)

            if addr_type == socket.AF_UNSPEC:
                assert any(_[0] == socket.AF_INET6 for _ in res)
                assert any(_[0] == socket.AF_INET for _ in res)
            elif addr_type == socket.AF_INET:
                assert all(_[0] == socket.AF_INET for _ in res)
            elif addr_type == socket.AF_INET6:
                assert all(_[0] == socket.AF_INET6 for _ in res)

    await resolver.close()


@requires_network()
@pytest.mark.parametrize(
    "dns_url",
    [
        "doh+google://",
        "doh+cloudflare://",
        _DOQ_PARAM,
        "dot://one.one.one.one",
        "dou://one.one.one.one",
    ],
)
@pytest.mark.asyncio
async def test_task_safe_resolver(dns_url: str) -> None:
    resolver = AsyncResolverDescription.from_url(dns_url).new()

    tasks = []

    for name in [
        "www.google.com",
        "www.cloudflare.com",
        "youtube.com",
        "github.com",
        "api.github.com",
    ]:
        tasks.append(
            resolver.getaddrinfo(
                name,
                443,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
        )

    results = await asyncio.gather(*tasks)

    for result in results:
        assert len(result)

    await resolver.close()


@requires_network()
@pytest.mark.asyncio
async def test_many_resolver_task_safe() -> None:
    resolvers = [
        AsyncResolverDescription.from_url("doh+google://").new(),
        AsyncResolverDescription.from_url("doh+cloudflare://").new(),
        AsyncResolverDescription.from_url("doh+adguard://").new(),
        AsyncResolverDescription.from_url("dot+google://").new(),
        AsyncResolverDescription.from_url("doh+google://").new(),
    ]

    resolver = AsyncManyResolver(*resolvers)

    tasks = []

    for name in [
        "www.google.com",
        "www.cloudflare.com",
        "youtube.com",
        "github.com",
        "api.github.com",
        "gist.github.com",
    ]:
        tasks.append(
            resolver.getaddrinfo(
                name,
                443,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
        )

    results = await asyncio.gather(*tasks)

    for result in results:
        assert len(result)

    await resolver.close()


@requires_network()
@pytest.mark.parametrize(
    "dns_url",
    [
        "doh+google://",
        "doh+cloudflare://",
        _DOQ_PARAM,
        "dot://one.one.one.one",
        "dou://one.one.one.one",
    ],
)
@pytest.mark.asyncio
@pytest.mark.parametrize("constrained", [False, True])
async def test_resolver_recycle(dns_url: str, constrained: bool) -> None:
    if constrained:
        dns_url += ("&" if "?" in dns_url else "?") + "hosts=*.test"
    resolver = AsyncResolverDescription.from_url(dns_url).new()

    await resolver.close()

    old_resolver, resolver = resolver, resolver.recycle()

    assert type(old_resolver) is type(resolver)

    assert resolver.protocol == old_resolver.protocol
    assert resolver.specifier == old_resolver.specifier
    assert resolver.implementation == old_resolver.implementation

    assert resolver.is_available()
    assert not old_resolver.is_available()

    assert resolver.have_constraints() is constrained
    if constrained:
        assert resolver.support("example.test") is True
        assert resolver.support("outside.invalid") is False

    await resolver.close()

    assert not resolver.is_available()


@requires_network()
@pytest.mark.parametrize(
    "dns_url",
    [
        "doh+google://",
        "doh+cloudflare://",
        _DOQ_PARAM,
        "dot://one.one.one.one",
        "dou://one.one.one.one",
    ],
)
@pytest.mark.asyncio
async def test_resolve_cannot_recycle_when_available(dns_url: str) -> None:
    resolver = AsyncResolverDescription.from_url(dns_url).new()

    with pytest.raises(RuntimeError):
        resolver.recycle()

    await resolver.close()


@requires_network()
@pytest.mark.parametrize(
    "dns_url",
    [
        "doh+google://",
        "doh+cloudflare://",
        _DOQ_PARAM,
        "dot://one.one.one.one",
        "dou://one.one.one.one",
    ],
)
@pytest.mark.asyncio
async def test_ipv6_always_preferred(dns_url: str) -> None:
    """Our resolvers must place IPV6 address in the beginning of returned list."""
    resolver = AsyncResolverDescription.from_url(dns_url).new()

    inet_classes = []

    res = await resolver.getaddrinfo(
        "www.cloudflare.com",
        443,
        socket.AF_UNSPEC,
        socket.SOCK_STREAM,
    )

    for r in res:
        if r[0] not in inet_classes:
            inet_classes.append(r[0])

    assert inet_classes[0] == socket.AF_INET6
    assert inet_classes[1] == socket.AF_INET

    await resolver.close()


@requires_network()
@pytest.mark.parametrize(
    "dns_url",
    [
        "doh+google://",
        "doh+cloudflare://",
        _DOQ_PARAM,
        "dot://one.one.one.one",
        "dou://one.one.one.one",
    ],
)
@pytest.mark.asyncio
async def test_dgram_upgrade(dns_url: str) -> None:
    """www.cloudflare.com records HTTPS exist, we know it. This verify that we are able to propose a DGRAM upgrade."""
    resolver = AsyncResolverDescription.from_url(dns_url).new()

    sock_types = []

    res = await resolver.getaddrinfo(
        "www.cloudflare.com",
        443,
        socket.AF_UNSPEC,
        socket.SOCK_STREAM,
        quic_upgrade_via_dns_rr=True,
    )

    for r in res:
        if r[1] not in sock_types:
            sock_types.append(r[1])

    assert sock_types[0] == socket.SOCK_DGRAM
    assert sock_types[1] == socket.SOCK_STREAM

    await resolver.close()


@requires_network()
@pytest.mark.parametrize("dns_url", [_DOQ_PARAM])
@pytest.mark.parametrize(
    "socktype, upgrade",
    [
        (socket.SOCK_STREAM, False),
        (socket.SOCK_STREAM, True),
        (socket.SOCK_DGRAM, True),
    ],
)
@pytest.mark.asyncio
async def test_doq_https_records(
    dns_url: str, socktype: socket.SocketKind, upgrade: bool
) -> None:
    resolver = AsyncResolverDescription.from_url(dns_url).new()
    try:
        results = await resolver.getaddrinfo(
            "encryptedsni.com",
            443,
            socket.AF_UNSPEC,
            socktype,
            quic_upgrade_via_dns_rr=upgrade,
        )
        assert results
        assert any(r[1] == socktype for r in results)
        for family, kind, protocol, ech, address in results:
            socket.inet_pton(family, address[0])
            assert address[1] == 443
            assert protocol == (6 if kind == socket.SOCK_STREAM else 17)
            if not upgrade or socktype == socket.SOCK_DGRAM:
                assert kind == socktype
            # ECH publication and key rotation belong to the remote zone.
            # When present, preserve its framed bytes for either socket kind.
            if ech:
                assert isinstance(ech, bytes)
                assert len(ech) > 2
                assert int.from_bytes(ech[:2], "big") == len(ech) - 2
            else:
                assert ech == ""
        assert resolver.is_available()
    finally:
        await resolver.close()
    assert not resolver.is_available()


@requires_network()
@pytest.mark.parametrize("dns_url", [_DOQ_PARAM])
@pytest.mark.asyncio
async def test_doq_reuse_after_nxdomain(dns_url: str) -> None:
    resolver = AsyncResolverDescription.from_url(dns_url).new()
    try:
        # .invalid is reserved, so this does not depend on a domain registration.
        with pytest.raises(socket.gaierror, match="DNS returned an error"):
            await resolver.getaddrinfo(
                "urllib3.invalid", 443, socket.AF_UNSPEC, socket.SOCK_STREAM
            )
        assert resolver.is_available()
        results = await resolver.getaddrinfo(
            "one.one.one.one", 443, socket.AF_INET, socket.SOCK_STREAM
        )
        assert any(r[4][0] in ("1.1.1.1", "1.0.0.1") for r in results)
    finally:
        await resolver.close()


@pytest.mark.parametrize(
    "dns_url, hostname, expected_addr",
    [
        (
            "in-memory://default/?hosts=abc.tld:1.1.1.1,def.tld:8.8.8.8",
            "abc.tld",
            "1.1.1.1",
        ),
        (
            "in-memory://default/?hosts=abc.tld:1.1.1.1,def.tld:8.8.8.8",
            "def.tld",
            "8.8.8.8",
        ),
        (
            "in-memory://default/?hosts=abc.tld:1.1.1.1,def.tld:8.8.8.8",
            "defe.tld",
            None,
        ),
        (
            "in-memory://default/?hosts=abc.tld:1.1.1.1,def.tld:8.8.8.8&hosts=a.company.internal:1.1.1.8",
            "a.company.internal",
            "1.1.1.8",
        ),
        (
            "in-memory://default/?hosts=abc.tld:1.1.1.1,def.tld:8.8.8.8&hosts=a.company.internal:1.1.1.8",
            "def.tld",
            "8.8.8.8",
        ),
        (
            "in-memory://default",
            "abc.tld",
            None,
        ),
        (
            "in-memory://default/?hosts=x",
            "abc.tld",
            None,
        ),
        (
            "in-memory://default/?hosts=x",
            "x",
            None,
        ),
        (
            "in-memory://default/?hosts=abc.tld:::1,def.tld:8.8.8.8",
            "abc.tld",
            "::1",
        ),
        (
            "in-memory://default/?hosts=abc.tld:[::1],def.tld:8.8.8.8",
            "abc.tld",
            "::1",
        ),
    ],
)
@pytest.mark.asyncio
async def test_in_memory_resolver(
    dns_url: str, hostname: str, expected_addr: str | None
) -> None:
    resolver = AsyncResolverDescription.from_url(dns_url).new()

    assert resolver.have_constraints()

    if expected_addr is None:
        with pytest.raises(socket.gaierror):
            await resolver.getaddrinfo(
                hostname,
                80,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
        return

    res = await resolver.getaddrinfo(
        hostname,
        80,
        socket.AF_UNSPEC,
        socket.SOCK_STREAM,
    )

    assert any([_[-1][0] == expected_addr for _ in res])


@requires_network()
@pytest.mark.asyncio
async def test_doh_http11() -> None:
    """Ensure we can do DoH over HTTP/1.1 even if... that's absolutely not recommended!"""
    resolver = AsyncResolverDescription.from_url(
        "doh+google://default/?disabled_svn=h2,h3"
    ).new()

    res = await resolver.getaddrinfo(
        "www.cloudflare.com",
        80,
        socket.AF_UNSPEC,
        socket.SOCK_STREAM,
    )

    assert len(res)

    await resolver.close()


@requires_network()
@pytest.mark.asyncio
async def test_doh_http11_upgradable() -> None:
    """Ensure we can do DoH over HTTP/1.1 that can upgrade to HTTP/3"""
    resolver = AsyncResolverDescription.from_url(
        "doh+google://default/?disabled_svn=h2&cert_reqs=0"
    ).new()
    with pytest.warns(InsecureRequestWarning):
        res = await resolver.getaddrinfo(
            "www.cloudflare.com",
            80,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )

        assert len(res)
    await resolver.close()


@requires_network()
@pytest.mark.asyncio
async def test_doh_on_connection_callback() -> None:
    """Ensure we can inspect the resolver connection with a callback."""
    resolver_description = AsyncResolverDescription.from_url("doh+google://")

    toggle_witness: bool = False

    async def callback(conn_info: ConnectionInfo) -> None:
        nonlocal toggle_witness
        if conn_info:
            toggle_witness = True

    resolver_description["on_post_connection"] = callback

    resolver = resolver_description.new()

    res = await resolver.getaddrinfo(
        "www.cloudflare.com",
        80,
        socket.AF_UNSPEC,
        socket.SOCK_STREAM,
    )

    assert len(res)
    assert toggle_witness


@pytest.mark.parametrize("dns_url", ["system://", "in-memory://", "null://"])
@pytest.mark.asyncio
async def test_not_closeable_recycle(dns_url: str) -> None:
    r = AsyncResolverDescription.from_url(dns_url).new()

    await r.close()

    assert r.is_available()

    rr = r.recycle()

    assert rr == r


@pytest.mark.asyncio
async def test_recycle_in_memory_with_mock() -> None:
    r = AsyncResolverDescription.from_url(
        "in-memory://default/?hosts=localhost:8.8.8.8&hosts=local:1.1.1.1&maxsize=8"
    ).new()

    assert r.is_available()
    assert len(r._hosts) == 2  # type: ignore[attr-defined]
    assert "localhost" in r._hosts  # type: ignore[attr-defined]
    assert "local" in r._hosts  # type: ignore[attr-defined]
    assert r._maxsize == 8  # type: ignore[attr-defined]

    await r.close()

    assert r.is_available()

    r.is_available = lambda: False  # type: ignore

    assert not r.is_available()

    rr = AsyncBaseResolver.recycle(r)

    assert rr.is_available()
    assert len(rr._hosts) == 2  # type: ignore[attr-defined]
    assert "localhost" in rr._hosts  # type: ignore[attr-defined]
    assert "local" in rr._hosts  # type: ignore[attr-defined]
    assert rr._maxsize == 8  # type: ignore[attr-defined]


@requires_network()
@pytest.mark.parametrize(
    "dns_url",
    [
        "null://default",
        "system://default",
        "in-memory://default",
        "dou://1.1.1.1",
        "doh+google://default",
        "dot://1.1.1.1",
        pytest.param(
            "doq://dns.adguard-dns.com/?timeout=5&cert_reqs=0",
            marks=[
                pytest.mark.skipif(
                    QUICResolver is _MISSING_QUIC_SENTINEL,
                    reason="Test requires qh3 installed",
                ),
            ],
        ),
    ],
)
@pytest.mark.parametrize(
    "ipv4_addr",
    [
        "1.1.1.1",
        "127.0.0.1",
        "192.168.1.1",
        "10.0.0.1",
        "255.255.255.255",
        "0.0.0.0",
    ],
)
@pytest.mark.asyncio
async def test_ipv4_passthrough(dns_url: str, ipv4_addr: str) -> None:
    """All resolvers must pass through raw IPv4 addresses without performing DNS resolution."""
    resolver = AsyncResolverDescription.from_url(dns_url).new()

    res = await resolver.getaddrinfo(
        ipv4_addr,
        80,
        socket.AF_UNSPEC,
        socket.SOCK_STREAM,
    )

    assert len(res) == 1
    assert res[0][0] == socket.AF_INET
    assert res[0][1] == socket.SOCK_STREAM
    assert res[0][-1][0] == ipv4_addr
    assert res[0][-1][1] == 80

    await resolver.close()


@requires_network()
@pytest.mark.parametrize(
    "dns_url",
    [
        "null://default",
        "system://default",
        "in-memory://default",
        "dou://1.1.1.1",
        "doh+google://default",
        "dot://1.1.1.1",
        pytest.param(
            "doq://dns.adguard-dns.com/?timeout=5&cert_reqs=0",
            marks=[
                pytest.mark.skipif(
                    QUICResolver is _MISSING_QUIC_SENTINEL,
                    reason="Test requires qh3 installed",
                ),
            ],
        ),
    ],
)
@pytest.mark.parametrize(
    "ipv4_addr",
    [
        "1.1.1.1",
        "127.0.0.1",
        "192.168.1.1",
    ],
)
@pytest.mark.asyncio
async def test_ipv4_passthrough_with_af_inet(dns_url: str, ipv4_addr: str) -> None:
    """Passing an IPv4 address with AF_INET family should succeed."""
    resolver = AsyncResolverDescription.from_url(dns_url).new()

    res = await resolver.getaddrinfo(
        ipv4_addr,
        443,
        socket.AF_INET,
        socket.SOCK_STREAM,
    )

    assert len(res) >= 1
    assert all(r[0] == socket.AF_INET for r in res)
    assert any(r[-1][0] == ipv4_addr for r in res)

    await resolver.close()


@requires_network()
@pytest.mark.parametrize(
    "dns_url",
    [
        "null://default",
        "in-memory://default",
        "dou://1.1.1.1",
        "doh+google://default",
        "dot://1.1.1.1",
        pytest.param(
            "doq://dns.adguard-dns.com/?timeout=5&cert_reqs=0",
            marks=[
                pytest.mark.skipif(
                    QUICResolver is _MISSING_QUIC_SENTINEL,
                    reason="Test requires qh3 installed",
                ),
            ],
        ),
    ],
)
@pytest.mark.parametrize(
    "ipv4_addr",
    [
        "1.1.1.1",
        "127.0.0.1",
    ],
)
@pytest.mark.asyncio
async def test_ipv4_passthrough_with_af_inet6_raises(
    dns_url: str, ipv4_addr: str
) -> None:
    """Passing an IPv4 address with AF_INET6 family must raise socket.gaierror."""
    resolver = AsyncResolverDescription.from_url(dns_url).new()

    with pytest.raises(
        socket.gaierror, match="Address family for hostname not supported"
    ):
        await resolver.getaddrinfo(
            ipv4_addr,
            80,
            socket.AF_INET6,
            socket.SOCK_STREAM,
        )

    await resolver.close()


@requires_network()
@pytest.mark.parametrize(
    "dns_url",
    [
        "null://default",
        "system://default",
        "in-memory://default",
        "dou://1.1.1.1",
        "doh+google://default",
        "dot://1.1.1.1",
        pytest.param(
            "doq://dns.adguard-dns.com/?timeout=5&cert_reqs=0",
            marks=[
                pytest.mark.skipif(
                    QUICResolver is _MISSING_QUIC_SENTINEL,
                    reason="Test requires qh3 installed",
                ),
            ],
        ),
    ],
)
@pytest.mark.parametrize(
    "ipv6_addr",
    [
        "::1",
        "::ffff:127.0.0.1",
        "2606:4700:4700::1111",
        "fe80::1",
        "fd12:3456:789a::1",
        "::",
    ],
)
@pytest.mark.asyncio
async def test_ipv6_passthrough(dns_url: str, ipv6_addr: str) -> None:
    """All resolvers must pass through raw IPv6 addresses without performing DNS resolution."""
    resolver = AsyncResolverDescription.from_url(dns_url).new()

    res = await resolver.getaddrinfo(
        ipv6_addr,
        80,
        socket.AF_UNSPEC,
        socket.SOCK_STREAM,
    )

    assert len(res) >= 1
    assert all(r[0] == socket.AF_INET6 for r in res)
    assert any(r[-1][0] == ipv6_addr for r in res)
    assert all(r[-1][1] == 80 for r in res)

    await resolver.close()


@requires_network()
@pytest.mark.parametrize(
    "dns_url",
    [
        "null://default",
        "system://default",
        "in-memory://default",
        "dou://1.1.1.1",
        "doh+google://default",
        "dot://1.1.1.1",
        pytest.param(
            "doq://dns.adguard-dns.com/?timeout=5&cert_reqs=0",
            marks=[
                pytest.mark.skipif(
                    QUICResolver is _MISSING_QUIC_SENTINEL,
                    reason="Test requires qh3 installed",
                ),
            ],
        ),
    ],
)
@pytest.mark.parametrize(
    "ipv6_addr",
    [
        "::1",
        "2606:4700:4700::1111",
        "fe80::1",
    ],
)
@pytest.mark.asyncio
async def test_ipv6_passthrough_with_af_inet6(dns_url: str, ipv6_addr: str) -> None:
    """Passing an IPv6 address with AF_INET6 family should succeed."""
    resolver = AsyncResolverDescription.from_url(dns_url).new()

    res = await resolver.getaddrinfo(
        ipv6_addr,
        443,
        socket.AF_INET6,
        socket.SOCK_STREAM,
    )

    assert len(res) >= 1
    assert all(r[0] == socket.AF_INET6 for r in res)
    assert any(r[-1][0] == ipv6_addr for r in res)

    await resolver.close()


@requires_network()
@pytest.mark.parametrize(
    "dns_url",
    [
        "null://default",
        "in-memory://default",
        "dou://1.1.1.1",
        "doh+google://default",
        "dot://1.1.1.1",
        pytest.param(
            "doq://dns.adguard-dns.com/?timeout=5&cert_reqs=0",
            marks=[
                pytest.mark.skipif(
                    QUICResolver is _MISSING_QUIC_SENTINEL,
                    reason="Test requires qh3 installed",
                ),
            ],
        ),
    ],
)
@pytest.mark.parametrize(
    "ipv6_addr",
    [
        "::1",
        "2606:4700:4700::1111",
    ],
)
@pytest.mark.asyncio
async def test_ipv6_passthrough_with_af_inet_raises(
    dns_url: str, ipv6_addr: str
) -> None:
    """Passing an IPv6 address with AF_INET family must raise socket.gaierror."""
    resolver = AsyncResolverDescription.from_url(dns_url).new()

    with pytest.raises(
        socket.gaierror, match="Address family for hostname not supported"
    ):
        await resolver.getaddrinfo(
            ipv6_addr,
            80,
            socket.AF_INET,
            socket.SOCK_STREAM,
        )

    await resolver.close()


@pytest.mark.parametrize("family", [socket.AF_INET, socket.AF_INET6])
@pytest.mark.asyncio
async def test_udp_cache_normalizes_host_and_rebinds_port(
    dns_udp_server: DNSUDPServer, family: socket.AddressFamily
) -> None:
    resolver = PlainResolver(*dns_udp_server.address, timeout=5)
    try:
        first = await resolver.getaddrinfo("CACHE.test", 80, family, socket.SOCK_STREAM)
        expected_ip = "192.0.2.1" if family == socket.AF_INET else "2001:db8::1"
        assert first[0][4][:2] == (expected_ip, 80)
        # Caller mutations must not affect cached records.
        first.clear()
        second = await resolver.getaddrinfo(
            b"cache.test", 443, family, socket.SOCK_STREAM
        )
        assert second[0][4][:2] == (expected_ip, 443)
        assert len(dns_udp_server.requests) == 2  # Address and HTTPS queries.
    finally:
        await resolver.close()


@pytest.mark.parametrize(
    "dns_udp_server, cache_max_ttl, expected_queries",
    [(60, 60, 1), (0, 60, 2), (2**31, 60, 2), (60, 0, 2)],
    indirect=["dns_udp_server"],
)
@pytest.mark.asyncio
async def test_udp_cache_respects_ttl(
    dns_udp_server: DNSUDPServer, cache_max_ttl: int, expected_queries: int
) -> None:
    resolver = PlainResolver(
        *dns_udp_server.address, timeout=5, cache_max_ttl=cache_max_ttl
    )
    try:
        for _ in range(2):
            result = await resolver.getaddrinfo(
                "ttl.test", 443, socket.AF_INET, socket.SOCK_STREAM
            )
            assert result[0][4] == ("192.0.2.1", 443)
        assert len(dns_udp_server.requests) == 2 * expected_queries
    finally:
        await resolver.close()


@pytest.mark.parametrize("maxsize, expected_queries", [(0, 6), (1, 6), (2, 4)])
@pytest.mark.asyncio
async def test_udp_cache_evicts_least_recently_used(
    dns_udp_server: DNSUDPServer, maxsize: int, expected_queries: int
) -> None:
    resolver = PlainResolver(*dns_udp_server.address, timeout=5, cache_maxsize=maxsize)
    try:
        for host in (
            "first.test",
            "second.test",
            "first.test",
            "third.test",
            "first.test",
            "second.test",
        ):
            assert await resolver.getaddrinfo(
                host, 443, socket.AF_INET, socket.SOCK_STREAM
            )
        assert len(dns_udp_server.requests) == 2 * expected_queries
    finally:
        await resolver.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_first", [False, True])
async def test_udp_cache_coalesces_concurrent_lookups(
    dns_udp_server: DNSUDPServer, cancel_first: bool
) -> None:
    resolver = PlainResolver(*dns_udp_server.address, timeout=5)
    dns_udp_server.respond.clear()
    pending = []
    try:
        first = asyncio.ensure_future(
            resolver.getaddrinfo("shared.test", 80, socket.AF_INET, socket.SOCK_STREAM)
        )
        pending.append(first)
        assert await asyncio.get_running_loop().run_in_executor(
            None, dns_udp_server.received.wait, 5
        )
        second = asyncio.ensure_future(
            resolver.getaddrinfo("shared.test", 443, socket.AF_INET, socket.SOCK_STREAM)
        )
        pending.append(second)
        # Let the second caller join the lookup while the server still holds its reply.
        await asyncio.sleep(0)
        if cancel_first:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        dns_udp_server.respond.set()
        assert (await asyncio.wait_for(second, 5))[0][4] == ("192.0.2.1", 443)
        if not cancel_first:
            assert (await asyncio.wait_for(first, 5))[0][4] == ("192.0.2.1", 80)
        assert len(dns_udp_server.requests) == 2
    finally:
        dns_udp_server.respond.set()
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await resolver.close()


@pytest.mark.asyncio
async def test_udp_cache_expires(dns_udp_server: DNSUDPServer) -> None:
    resolver = PlainResolver(*dns_udp_server.address, timeout=5, cache_max_ttl=1)
    try:
        first = await resolver.getaddrinfo(
            "expire.test", 443, socket.AF_INET, socket.SOCK_STREAM
        )
        await asyncio.sleep(1.05)
        assert (
            await resolver.getaddrinfo(
                "expire.test", 443, socket.AF_INET, socket.SOCK_STREAM
            )
            == first
        )
        assert len(dns_udp_server.requests) == 4
    finally:
        await resolver.close()


@pytest.mark.parametrize("rfc8484", [False, True])
@pytest.mark.parametrize("via_proxy", [False, True])
@pytest.mark.parametrize("multiple_headers", [False, True])
@pytest.mark.asyncio
async def test_doh_local_configuration(
    dns_https_server: DNSHTTPSServer,
    rfc8484: bool,
    via_proxy: bool,
    multiple_headers: bool,
) -> None:
    server = dns_https_server
    connections: list[ConnectionInfo] = []

    async def on_connection(info: ConnectionInfo) -> None:
        connections.append(info)

    headers = "Authorization:Bearer DoHSecret"
    proxy_headers = "Proxy-Authorization:Basic ProxySecret"
    proxy_options = {}
    if via_proxy:
        proxy_options = {
            "proxy": server.proxy_url,
            "proxy_headers": [proxy_headers, "X-Proxy:private"]
            if multiple_headers
            else proxy_headers,
        }
    resolver = HTTPSResolver(
        server.config.host,
        server.config.port,
        path="/dns-query" if rfc8484 else "/custom-resolve",
        rfc8484=rfc8484,
        source_address="127.0.0.1:0",
        headers=[headers, "X-DoH:first", "X-DoH:second", "Accept:ignored"]
        if multiple_headers
        else headers,
        disabled_svn=["h2", "h3"],
        ca_certs=server.config.ca_certs,
        timeout=5,
        retries=0,
        on_post_connection=on_connection,
        **proxy_options,
    )
    try:
        assert resolver.is_available()
        results = await resolver.getaddrinfo(
            "example.test", 443, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
        assert {(r[0], r[4]) for r in results} == {
            (socket.AF_INET, ("192.0.2.1", 443)),
            (socket.AF_INET6, ("2001:db8::1", 443, 0, 0)),
        }
        assert connections and all(
            c.http_version == HttpVersion.h11 for c in connections
        )
        assert len(server.requests) == 3
        for request in server.requests:
            assert request.headers["Authorization"] == "Bearer DoHSecret"
            assert request.headers["Accept"] == (
                "application/dns-message" if rfc8484 else "application/dns-json"
            )
            assert request.path == ("/dns-query" if rfc8484 else "/custom-resolve")
            assert request.remote_ip == "127.0.0.1"
            assert "Proxy-Authorization" not in request.headers
            if multiple_headers:
                assert [
                    value.strip() for value in request.headers["X-DoH"].split(",")
                ] == ["first", "second"]
                assert "X-Proxy" not in request.headers
        assert bool(server.proxy_requests) is via_proxy
        for request in server.proxy_requests:
            assert request.uri == f"{server.config.host}:{server.config.port}"
            assert request.headers["Proxy-Authorization"] == "Basic ProxySecret"
            assert "Authorization" not in request.headers
            if multiple_headers:
                assert request.headers["X-Proxy"] == "private"
    finally:
        await resolver.close()
    assert not resolver.is_available()


@pytest.mark.parametrize(
    "options, message",
    [
        ({"source_address": "127.0.0.1:bad"}, "invalid source_address"),
        ({"source_address": ":0"}, "invalid source_address"),
        ({"source_address": ("127.0.0.1", 0)}, "invalid source_address"),
        ({"headers": "MissingSeparator"}, "Passed header is invalid"),
        (
            {"proxy": "http://127.0.0.1:1", "proxy_headers": "MissingSeparator"},
            "Passed header is invalid",
        ),
    ],
)
def test_doh_local_invalid_configuration(
    options: dict[str, object], message: str
) -> None:
    # Invalid configuration must fail before any connection is attempted.
    with pytest.raises(ValueError, match=message):
        HTTPSResolver("127.0.0.1", None, **options)


@pytest.mark.parametrize(
    "dns_https_server, rfc8484, message",
    [
        ((503, b"DNS service unavailable"), False, "server response status 503"),
        ((200, {"Status": 3}), False, "Status 3"),
        (
            (200, {"Status": 2, "Comment": ["DNSSEC failed", "signature expired"]}),
            False,
            "DNSSEC failed, signature expired",
        ),
        ((200, {"Status": 0, "Question": []}), False, "Name or service not known"),
        # SERVFAIL, NXDOMAIN and an unknown RCODE with one example.test A question.
        *[
            (
                (
                    200,
                    bytes([0, 0, 0x81, 0x80 | rcode, 0, 1])
                    + b"\x00" * 6
                    + b"\x07example\x04test\x00\x00\x01\x00\x01",
                ),
                True,
                message,
            )
            for rcode, message in [
                (2, "DNSSEC validation failure"),
                (3, "DNS returned an error"),
                (15, "DNS returned an error: code 15"),
            ]
        ],
    ],
    indirect=["dns_https_server"],
)
@pytest.mark.asyncio
async def test_doh_local_errors(
    dns_https_server: DNSHTTPSServer, rfc8484: bool, message: str
) -> None:
    server = dns_https_server
    resolver = HTTPSResolver(
        server.config.host,
        server.config.port,
        rfc8484=rfc8484,
        ca_certs=server.config.ca_certs,
        timeout=5,
        retries=0,
        disabled_svn="h3",
    )
    try:
        with pytest.raises(socket.gaierror, match=message):
            await resolver.getaddrinfo(
                "example.test", 443, socket.AF_INET, socket.SOCK_STREAM
            )
        assert len(server.requests) == 2
    finally:
        await resolver.close()


@pytest.mark.parametrize(
    "representation", ["text", "hex", "wire", "invalid-ech", "invalid-hex"]
)
@pytest.mark.parametrize("alpn", ["h2", "h3"])
@pytest.mark.parametrize(
    "socktype, upgrade",
    [
        (socket.SOCK_STREAM, True),
        (socket.SOCK_STREAM, False),
        (socket.SOCK_DGRAM, True),
    ],
)
@pytest.mark.asyncio
async def test_doh_local_https_records(
    dns_https_server: DNSHTTPSServer,
    representation: str,
    alpn: str,
    socktype: socket.SocketKind,
    upgrade: bool,
) -> None:
    server = dns_https_server
    # A framed ECHConfigList with an unknown version: resolution preserves opaque bytes.
    ech = b"\x00\x05\xff\xff\x00\x01x"
    raw = b"\x00\x01\x00\x00\x01\x00\x03\x02" + alpn.encode()
    raw += struct.pack("!HH", 5, len(ech)) + ech
    if representation == "invalid-hex":
        server.https_records.append("\\# 1 zz")
    elif representation == "wire":
        server.https_records.append(raw)
    elif representation == "hex":
        server.https_records.append(f"\\# {len(raw)} {raw.hex()}")
    else:
        encoded_ech = (
            "invalid" if representation == "invalid-ech" else b64encode(ech).decode()
        )
        server.https_records.append(f"1 . alpn={alpn} ech={encoded_ech}")
    resolver = HTTPSResolver(
        server.config.host,
        server.config.port,
        rfc8484=representation == "wire",
        ca_certs=server.config.ca_certs,
        timeout=5,
        retries=0,
        disabled_svn=["h2", "h3"],
    )
    try:
        results = await resolver.getaddrinfo(
            "example.test",
            443,
            socket.AF_UNSPEC,
            socktype,
            quic_upgrade_via_dns_rr=upgrade,
        )
        expected_ech = "" if representation in ("invalid-ech", "invalid-hex") else ech
        expected = {
            (
                socket.AF_INET,
                socktype,
                6 if socktype == socket.SOCK_STREAM else 17,
                expected_ech,
                ("192.0.2.1", 443),
            ),
            (
                socket.AF_INET6,
                socktype,
                6 if socktype == socket.SOCK_STREAM else 17,
                expected_ech,
                ("2001:db8::1", 443, 0, 0),
            ),
        }
        if (
            upgrade
            and alpn == "h3"
            and socktype == socket.SOCK_STREAM
            and representation != "invalid-hex"
        ):
            expected |= {(r[0], socket.SOCK_DGRAM, 17, r[3], r[4]) for r in expected}
        assert set(results) == expected
        assert len(results) == len(expected)
        assert len(server.requests) == 3
        assert all(
            r.headers["Accept"]
            == (
                "application/dns-message"
                if representation == "wire"
                else "application/dns-json"
            )
            for r in server.requests
        )
    finally:
        await resolver.close()


@pytest.mark.parametrize(
    "userinfo, authorization",
    [
        ("User:Pass", "Basic VXNlcjpQYXNz"),
        ("User:pa:ss", "Basic VXNlcjpwYTpzcw=="),
        ("Us%65r:p%40ss", "Basic VXNlcjpwQHNz"),
        ("User:", "Basic VXNlcjo="),
        ("Token%2FX", "Bearer Token/X"),
        ("'User':'Pass'", "Basic VXNlcjpQYXNz"),
    ],
)
@pytest.mark.parametrize(
    "header_query",
    [
        "",
        "&headers=X-Case:MixedValue",
        "&headers=X-Case:MixedValue&headers=X-Other:OtherValue",
    ],
)
@pytest.mark.asyncio
async def test_resolver_url_authentication(
    dns_https_server: DNSHTTPSServer,
    userinfo: str,
    authorization: str,
    header_query: str,
) -> None:
    server = dns_https_server
    description = AsyncResolverDescription.from_url(
        f"doh://{userinfo}@{server.config.host}:{server.config.port}/resolve"
        f"?timeout=5&disabled_svn=h2,h3{header_query}"
    )
    description["ca_certs"] = server.config.ca_certs
    assert "ca_certs" in description and "missing" not in description
    resolver = description.new()
    try:
        assert (
            len(
                await resolver.getaddrinfo(
                    "example.test", 443, socket.AF_INET, socket.SOCK_STREAM
                )
            )
            == 1
        )
        assert len(server.requests) == 2
        for request in server.requests:
            assert request.headers["Authorization"] == authorization
            if header_query:
                assert request.headers["X-Case"] == "MixedValue"
            if "X-Other" in header_query:
                assert request.headers["X-Other"] == "OtherValue"
    finally:
        await resolver.close()


@pytest.mark.parametrize(
    "query",
    [
        "hosts=*.one.test&hosts=*.two.test,*.three.test",
        "hosts=*.one.test&HOSTS=*.two.test&HOSTS=*.three.test",
        "hosts=*.one.test,*.two.test&HOSTS=*.three.test,*.four.test",
        "hosts=*.one.test&HOSTS=*.two.test&Hosts=*.three.test",
        "hosts=*.one.test&HOSTS=*.two.test,*.three.test",
        "hosts=*.one.test,*.two.test&HOSTS=*.three.test",
        "hosts=*.one.test,*.two.test&HOSTS=*.three.test&HOSTS=*.four.test",
    ],
)
@pytest.mark.asyncio
async def test_resolver_url_host_constraints(query: str) -> None:
    description = AsyncResolverDescription.from_url(f"null://default?{query}")
    resolver = description.new()
    try:
        assert resolver.have_constraints()
        assert resolver.support("a.one.test")
        assert resolver.support("a.two.test")
        assert resolver.support("a.three.test")
        assert resolver.support("a.four.test") == ("four.test" in query)
        assert not resolver.support("outside.test")
    finally:
        await resolver.close()


def test_resolver_url_values_preserve_case() -> None:
    description = AsyncResolverDescription.from_url(
        "doh://localhost/CustomPath?timeout=0.25&maxsize=2&rfc8484=TRUE&cert_reqs=0"
        "&ca_certs=/Some/TrustRoot.pem&key_password=Secret&happy_eyeballs=false"
        "&disabled_svn=H2,H3&headers=X-Key:Value&implementation=urllib3"
    )
    assert description.implementation == "urllib3"
    assert description.kwargs == {
        "path": "/CustomPath",
        "timeout": 0.25,
        "maxsize": 2,
        "rfc8484": True,
        "cert_reqs": 0,
        "ca_certs": "/Some/TrustRoot.pem",
        "key_password": "Secret",
        "happy_eyeballs": False,
        "disabled_svn": ["H2", "H3"],
        "headers": "X-Key:Value",
    }


@pytest.mark.parametrize(
    "url, error",
    [
        ("localhost", "missing a protocol"),
        (
            "doh://localhost?implementation=urllib3&IMPLEMENTATION=other",
            "Only one implementation",
        ),
        (
            "doh://localhost?implementation=urllib3&implementation=other",
            "Only one implementation",
        ),
        (
            "doh://localhost?IMPLEMENTATION=urllib3&IMPLEMENTATION=other",
            "Only one implementation",
        ),
    ],
)
def test_resolver_url_invalid_description(url: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        AsyncResolverDescription.from_url(url)


@pytest.mark.parametrize(
    "specifier, implementation", [("missing", None), (None, "missing")]
)
def test_resolver_factory_unavailable(
    specifier: str | None, implementation: str | None
) -> None:
    with pytest.raises(NotImplementedError, match="cannot be loaded"):
        AsyncResolverFactory.new(
            ProtocolResolver.SYSTEM, specifier=specifier, implementation=implementation
        )


@pytest.mark.parametrize(
    "addresses, expected",
    [
        (["1.1.1.1", "11.1.1.1", "1.1.1.1"], ["1.1.1.1", "11.1.1.1"]),
        (
            ["2001:db8::1", "2001:db8::10", "2001:db8::1"],
            ["2001:db8::1", "2001:db8::10"],
        ),
        (["[::1]", "::1", "[::1]"], ["::1"]),
        (["[fe80::1%EthA]", "[fe80::1%EthA]"], ["fe80::1%EthA"]),
    ],
)
@pytest.mark.asyncio
async def test_in_memory_registration(
    addresses: list[str], expected: list[str]
) -> None:
    resolver = InMemoryResolver()
    for address in addresses:
        resolver.register("example.test", address)
    records = await resolver.getaddrinfo(
        "example.test", 80, socket.AF_UNSPEC, socket.SOCK_STREAM
    )
    assert [record[4][0] for record in records] == expected
    assert all(
        record[0] == (socket.AF_INET6 if ":" in record[4][0] else socket.AF_INET)
        for record in records
    )
    resolver.clear("example.test")
    resolver.clear("missing.test")
    assert not resolver.support("example.test")
    with pytest.raises(socket.gaierror, match="no records found"):
        await resolver.getaddrinfo(
            "example.test", 80, socket.AF_UNSPEC, socket.SOCK_STREAM
        )


@pytest.mark.parametrize("maxsize", [0, 1, 2])
@pytest.mark.asyncio
async def test_in_memory_capacity(maxsize: int) -> None:
    resolver = InMemoryResolver(maxsize=maxsize)
    for name in ("first.test", "second.test", "third.test"):
        resolver.register(name, "192.0.2.1")
    assert not resolver.support("first.test")
    assert resolver.support("second.test") == (maxsize == 2)
    assert resolver.support("third.test") == (maxsize > 0)
    if maxsize:
        assert (
            await resolver.getaddrinfo(
                "third.test", 80, socket.AF_INET, socket.SOCK_STREAM
            )
        )[0][4] == ("192.0.2.1", 80)


@pytest.mark.parametrize("binary_ech", [False, True])
@pytest.mark.asyncio
async def test_in_memory_configuration_and_family_filter(binary_ech: bool) -> None:
    ech = b"\x00\x05\xff\xff\x00\x01x"
    resolver = InMemoryResolver(
        "localhost:192.0.2.1",
        "localhost:[2001:db8::1]",
        "ipv4.test:192.0.2.2",
        "ignored-pattern",
        server="unused",
        port=53,
        ech_config=ech if binary_ech else ech.hex(),
    )
    assert resolver.have_constraints()
    assert resolver.support(None) and resolver.support(b"localhost")
    assert not resolver.support("ignored-pattern")
    for family, address in [
        (socket.AF_INET, ("192.0.2.1", 80)),
        (socket.AF_INET6, ("2001:db8::1", 80, 0, 0)),
    ]:
        assert await resolver.getaddrinfo(
            "localhost", 80, family, socket.SOCK_DGRAM
        ) == [(family, socket.SOCK_DGRAM, 17, ech, address)]
    with pytest.raises(socket.gaierror, match="Name or service not known"):
        await resolver.getaddrinfo("ipv4.test", 80, socket.AF_INET6, socket.SOCK_STREAM)
    await resolver.close()
    assert resolver.is_available() and resolver.recycle() is resolver


@pytest.mark.parametrize(
    "specifier, implementation, available",
    [
        (None, None, True),
        (None, "socket", True),
        ("missing", None, False),
        (None, "missing", False),
    ],
)
def test_async_resolver_factory_has(
    specifier: str | None, implementation: str | None, available: bool
) -> None:
    assert (
        AsyncResolverFactory.has(ProtocolResolver.SYSTEM, specifier, implementation)
        is available
    )


@pytest.mark.asyncio
async def test_in_memory_url_host_case_and_ipv6_zone() -> None:
    resolver = AsyncResolverDescription.from_url(
        "in-memory://default?hosts=Example.TEST:[fe80::1%25EthA]"
    ).new()
    assert isinstance(resolver, InMemoryResolver)
    assert resolver.support(b"EXAMPLE.test")
    assert await resolver.getaddrinfo(
        "example.test", 80, socket.AF_INET6, socket.SOCK_STREAM
    ) == [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1%EthA", 80, 0, 0)),
    ]
    resolver.clear("EXAMPLE.TEST")
    assert not resolver.support("example.test")
    await resolver.close()


def test_resolver_url_query_path_override() -> None:
    description = AsyncResolverDescription.from_url(
        "doh://localhost/resolve?path=/CustomPath"
    )
    assert description.kwargs["path"] == "/CustomPath"


@pytest.mark.parametrize(
    "hostname, expected",
    [
        (b"known.private.test", "198.51.100.8"),
        ("unknown.private.test", None),
        (b"public.test", "192.0.2.1"),
        (None, "127.0.0.2"),
    ],
)
@pytest.mark.asyncio
async def test_many_resolver_local_routing(
    dns_https_server: DNSHTTPSServer,
    hostname: str | bytes | None,
    expected: str | None,
) -> None:
    server = dns_https_server
    fallback = HTTPSResolver(
        server.config.host,
        server.config.port,
        ca_certs=server.config.ca_certs,
        timeout=5,
        retries=0,
        disabled_svn=["h2", "h3"],
    )
    # Constrained resolvers take precedence even when listed after the fallback.
    resolver = AsyncManyResolver(
        NullResolver(),
        fallback,
        NullResolver("*.private.test"),
        InMemoryResolver("known.private.test:198.51.100.8", "localhost:127.0.0.2"),
    )
    try:
        if expected is None:
            with pytest.raises(socket.gaierror, match="Name or service not known"):
                await resolver.getaddrinfo(
                    hostname, 443, socket.AF_INET, socket.SOCK_STREAM
                )
        else:
            results = await resolver.getaddrinfo(
                hostname, 443, socket.AF_INET, socket.SOCK_STREAM
            )
            assert [result[-1] for result in results] == [(expected, 443)]
        if hostname == b"public.test":
            assert len(server.requests) == 2
            assert all(
                req.query_arguments["name"] == [b"public.test"]
                for req in server.requests
            )
        else:
            # Failed private lookups must not leak to the unrestricted endpoint.
            assert not server.requests
    finally:
        await resolver.close()


@pytest.mark.parametrize(
    "dns_https_server, message",
    [
        (
            (200, {"Status": 2, "Comment": "DNSSEC validation failed"}),
            "DNSSEC validation failed",
        ),
        ((200, {"Status": 2, "Comment": "DNSKEY missing"}), "DNSKEY missing"),
    ],
    indirect=["dns_https_server"],
)
@pytest.mark.parametrize("constrained", [False, True])
@pytest.mark.asyncio
async def test_many_resolver_local_dnssec(
    dns_https_server: DNSHTTPSServer, message: str, constrained: bool
) -> None:
    server = dns_https_server
    children = [
        HTTPSResolver(
            server.config.host,
            server.config.port,
            *(("*.test",) if constrained else ()),
            path=path,
            ca_certs=server.config.ca_certs,
            timeout=5,
            retries=0,
            disabled_svn=["h2", "h3"],
        )
        for path in ("/primary", "/fallback")
    ]
    resolver = AsyncManyResolver(*children)
    try:
        with pytest.raises(socket.gaierror, match=message):
            await resolver.getaddrinfo(
                "example.test", 443, socket.AF_INET, socket.SOCK_STREAM
            )
        assert len(server.requests) == 2
        assert all(req.path == "/primary" for req in server.requests)
    finally:
        await resolver.close()


@pytest.mark.parametrize("recycle_composite", [False, True])
@pytest.mark.parametrize("constrained", [False, True])
@pytest.mark.parametrize("rfc8484", [False, True])
@pytest.mark.asyncio
async def test_many_resolver_local_recycle(
    dns_https_server: DNSHTTPSServer,
    recycle_composite: bool,
    constrained: bool,
    rfc8484: bool,
) -> None:
    server = dns_https_server
    child = HTTPSResolver(
        server.config.host,
        server.config.port,
        *(("*.test",) if constrained else ()),
        path="/custom",
        headers="Authorization:Bearer CaseSensitive",
        rfc8484=rfc8484,
        ca_certs=server.config.ca_certs,
        timeout=5,
        retries=0,
        disabled_svn=["h2", "h3"],
    )
    resolver = AsyncManyResolver(child, InMemoryResolver("saved.local:198.51.100.9"))
    try:
        before = await resolver.getaddrinfo(
            "before.test", 443, socket.AF_INET, socket.SOCK_STREAM
        )
        assert [result[-1] for result in before] == [("192.0.2.1", 443)]
        if recycle_composite:
            await resolver.close()
            assert not resolver.is_available()
            recycled = resolver.recycle()
            assert isinstance(recycled, AsyncManyResolver)
            assert recycled is not resolver
            resolver = recycled
        else:
            await child.close()
        assert not child.is_available()
        assert resolver.is_available()
        after = await resolver.getaddrinfo(
            "after.test", 80, socket.AF_INET, socket.SOCK_STREAM
        )
        assert [result[-1] for result in after] == [("192.0.2.1", 80)]
        saved = await resolver.getaddrinfo(
            "saved.local", 80, socket.AF_INET, socket.SOCK_STREAM
        )
        assert [result[-1] for result in saved] == [("198.51.100.9", 80)]
        assert len(server.requests) == 4
        for req in server.requests:
            assert req.path == "/custom"
            assert req.headers["Authorization"] == "Bearer CaseSensitive"
            assert ("dns" in req.query_arguments) is rfc8484
        if constrained:
            with pytest.raises(socket.gaierror, match="Name or service not known"):
                await resolver.getaddrinfo(
                    "outside.invalid", 80, socket.AF_INET, socket.SOCK_STREAM
                )
            assert len(server.requests) == 4
    finally:
        await resolver.close()


@pytest.mark.asyncio
async def test_many_resolver_local_recycle_during_other_lookup(
    dns_https_server: DNSHTTPSServer, dns_udp_server: DNSUDPServer
) -> None:
    server = dns_https_server
    child = HTTPSResolver(
        server.config.host,
        server.config.port,
        ca_certs=server.config.ca_certs,
        timeout=5,
        retries=0,
        disabled_svn=["h2", "h3"],
    )
    await child.close()
    held = PlainResolver(*dns_udp_server.address, "*.private.test", timeout=5)
    resolver = AsyncManyResolver(child, NullResolver(), held)
    dns_udp_server.respond.clear()
    try:
        pending = asyncio.create_task(
            resolver.getaddrinfo(
                "held.private.test", 80, socket.AF_INET, socket.SOCK_STREAM
            )
        )
        try:
            assert await asyncio.get_running_loop().run_in_executor(
                None, dns_udp_server.received.wait, 5
            )
            # The active constrained lookup rotates this lookup to NullResolver;
            # falling back must recycle the closed, unrestricted DoH child.
            result = await resolver.getaddrinfo(
                "public.test", 443, socket.AF_INET, socket.SOCK_STREAM
            )
            assert [entry[-1] for entry in result] == [("192.0.2.1", 443)]
        finally:
            dns_udp_server.respond.set()
            result = await asyncio.wait_for(pending, 5)
        assert [entry[-1] for entry in result] == [("192.0.2.1", 80)]
        assert not child.is_available()
        assert len(server.requests) == 2
        assert all(
            req.query_arguments["name"] == [b"public.test"] for req in server.requests
        )
    finally:
        dns_udp_server.respond.set()
        await resolver.close()


@pytest.mark.parametrize("hostname", [None, b"localhost", b"service.example"])
def test_system_resolver_accepts_bytes_and_default_host(
    hostname: str | bytes | None,
) -> None:
    resolver = SystemResolver("*.example", server="ignored", port=853)
    assert resolver.support(hostname) is True
    assert resolver.support("elsewhere.invalid") is False


@pytest.mark.asyncio
async def test_create_connection_rejects_invalid_idna() -> None:
    from urllib3.exceptions import LocationParseError

    with pytest.raises(LocationParseError, match="label empty or too long"):
        await SystemResolver().create_connection(("a" * 64 + ".example", 80), timeout=1)


@pytest.mark.asyncio
async def test_create_connection_without_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib3.contrib.resolver._cache import AddrInfo

    async def getaddrinfo(*args: object, **kwargs: object) -> list[AddrInfo]:
        return []

    resolver = SystemResolver()
    monkeypatch.setattr(resolver, "getaddrinfo", getaddrinfo)
    with pytest.raises(OSError, match="getaddrinfo returns an empty list"):
        await resolver.create_connection(("service.invalid", 80), timeout=1)


@pytest.mark.asyncio
async def test_cancelled_cache_fill_can_be_retried() -> None:
    from urllib3.contrib.resolver._cache import AddrInfo, AsyncResolverCache, _make_key

    cache = AsyncResolverCache()
    key = _make_key(None, socket.AF_INET, socket.SOCK_STREAM, 0, 0, False)

    async def cancelled() -> list[AddrInfo]:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await cache.get_or_resolve(key, 80, cancelled)
    # The cancelled fill must be removed and its completion callback consumed.
    await asyncio.sleep(0)

    async def resolved() -> list[AddrInfo]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    assert await cache.get_or_resolve(key, 8080, resolved) == [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8080))
    ]


@pytest.mark.asyncio
async def test_create_connection_rejects_port_overflow() -> None:
    resolver = InMemoryResolver()
    resolver.register("local.example", "127.0.0.1")
    with pytest.raises(OverflowError):
        await resolver.create_connection(("local.example", 65536), timeout=1)


@pytest.mark.asyncio
async def test_create_connection_when_ipv6_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib3.util import connection

    monkeypatch.setattr(connection, "HAS_IPV6", False)
    finished = asyncio.Event()

    async def handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            assert await reader.readexactly(1) == b"x"
            writer.write(b"y")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        sock = await SystemResolver().create_connection(
            server.sockets[0].getsockname(), timeout=5
        )
        try:
            assert sock.family == socket.AF_INET
            await sock.sendall(b"x")
            assert await sock.recv(1) == b"y"
        finally:
            sock.close()
        await asyncio.wait_for(finished.wait(), 5)
    finally:
        server.close()
        await server.wait_closed()
