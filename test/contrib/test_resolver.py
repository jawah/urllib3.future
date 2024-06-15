from __future__ import annotations

import os
import socket
from concurrent.futures import ThreadPoolExecutor
from socket import AddressFamily, SocketKind
from test import requires_network

import pytest

from urllib3 import ConnectionInfo
from urllib3.contrib.resolver import (
    BaseResolver,
    ManyResolver,
    ProtocolResolver,
    ResolverDescription,
)
from urllib3.contrib.resolver.doh import HTTPSResolver
from urllib3.contrib.resolver.doq import QUICResolver
from urllib3.contrib.resolver.dot import TLSResolver
from urllib3.contrib.resolver.dou import PlainResolver
from urllib3.contrib.resolver.in_memory import InMemoryResolver
from urllib3.contrib.resolver.null import NullResolver
from urllib3.contrib.resolver.system import SystemResolver


@pytest.mark.parametrize(
    "hostname, expect_error",
    [
        ("abc.com", True),
        ("1.1.1.1", False),
        ("8.8.8.com", True),
        ("cloudflare.com", True),
    ],
)
def test_null_resolver(hostname: str, expect_error: bool) -> None:
    null_resolver = ResolverDescription(ProtocolResolver.NULL).new()

    if expect_error:
        with pytest.raises(socket.gaierror):
            null_resolver.getaddrinfo(
                hostname,
                80,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
    else:
        res = null_resolver.getaddrinfo(
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
            "doq://dns.nextdns.io/?timeout=1",
            QUICResolver,
            marks=pytest.mark.xfail(
                os.environ.get("CI", None) is not None,
                reason="Github Action CI Unpredictable",
                strict=False,
            ),
        ),
        ("dns://dns.nextdns.io", None),
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
            "doq://dns.nextdns.io/?implementation=qh3&timeout=1",
            QUICResolver,
            marks=pytest.mark.xfail(
                os.environ.get("CI", None) is not None,
                reason="Github Action CI Unpredictable",
                strict=False,
            ),
        ),
    ],
)
def test_url_resolver(
    url: str, expected_resolver_class: type[BaseResolver] | None
) -> None:
    if expected_resolver_class is None:
        with pytest.raises(
            (
                NotImplementedError,
                ValueError,
                TypeError,
            )
        ):
            ResolverDescription.from_url(url).new()
        return

    resolver = ResolverDescription.from_url(url).new()

    assert isinstance(resolver, expected_resolver_class)
    resolver.close()


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
        pytest.param(
            "doq://dns.nextdns.io/?timeout=1",
            marks=pytest.mark.xfail(
                os.environ.get("CI", None) is not None,
                reason="Github Action CI Unpredictable",
                strict=False,
            ),
        ),
        "doh+google://",
        "doh+cloudflare://default",
    ],
)
def test_1_1_1_1_ipv4_resolution_across_protocols(dns_url: str) -> None:
    resolver = ResolverDescription.from_url(dns_url).new()

    res = resolver.getaddrinfo(
        "one.one.one.one",
        443,
        socket.AF_INET,
        socket.SOCK_STREAM,
        quic_upgrade_via_dns_rr=False,
    )
    assert any([_[-1][0] in ["1.1.1.1", "1.0.0.1"] for _ in res])
    resolver.close()


@requires_network()
@pytest.mark.parametrize(
    "dns_url",
    [
        "dou://1.1.1.1",
        "dou://one.one.one.one",
        "dou://dns.google",
        "doh://cloudflare-dns.com/dns-query",
        "doh://dns.google",
        "dot://dns.google",
        "dot://one.one.one.one",
        pytest.param(
            "doq://dns.nextdns.io/?timeout=1",
            marks=pytest.mark.xfail(
                os.environ.get("CI", None) is not None,
                reason="Github Action CI Unpredictable",
                strict=False,
            ),
        ),
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
def test_dnssec_exception(dns_url: str, hostname: str, expected_failure: bool) -> None:
    resolver = ResolverDescription.from_url(dns_url).new()

    if expected_failure:
        with pytest.raises(socket.gaierror, match="DNSSEC|DNSKEY"):
            resolver.getaddrinfo(
                hostname,
                443,
                socket.AF_INET,
                socket.SOCK_STREAM,
                quic_upgrade_via_dns_rr=False,
            )
        resolver.close()
        return

    res = resolver.getaddrinfo(
        hostname,
        443,
        socket.AF_INET,
        socket.SOCK_STREAM,
        quic_upgrade_via_dns_rr=False,
    )

    assert len(res)
    resolver.close()


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
def test_hostname_too_long(dns_url: str, hostname: str) -> None:
    resolver = ResolverDescription.from_url(dns_url).new()

    with pytest.raises(
        UnicodeError, match="exceed 63 characters|exceed 253 characters|too long"
    ):
        resolver.getaddrinfo(
            hostname,
            80,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )

    resolver.close()


def test_many_resolver_host_constraint_distribution() -> None:
    resolvers = [
        ResolverDescription.from_url("system://default?hosts=localhost").new(),
        ResolverDescription.from_url("dou://127.0.0.1").new(),
        ResolverDescription.from_url("in-memory://").new(),
    ]

    assert resolvers[0].have_constraints()
    assert not resolvers[1].have_constraints()
    assert resolvers[2].have_constraints()

    imr = resolvers[-1]

    imr.register("notlocalhost", "127.5.5.1")  # type: ignore[attr-defined]
    imr.register("c.localhost.eu", "127.8.8.1")  # type: ignore[attr-defined]
    imr.register("c.localhost.eu", "::1")  # type: ignore[attr-defined]

    resolver = ManyResolver(*resolvers)

    res = resolver.getaddrinfo(
        "localhost",
        80,
        socket.AF_UNSPEC,
        socket.SOCK_STREAM,
    )

    assert len(res)
    assert any(_[-1][0] == "127.0.0.1" for _ in res)

    res = resolver.getaddrinfo(
        "notlocalhost",
        80,
        socket.AF_UNSPEC,
        socket.SOCK_STREAM,
    )

    assert len(res) == 1
    assert any(_[-1][0] == "127.5.5.1" for _ in res)

    res = resolver.getaddrinfo(
        "c.localhost.eu",
        80,
        socket.AF_UNSPEC,
        socket.SOCK_STREAM,
    )

    assert len(res) == 2
    assert any(_[-1][0] == "127.8.8.1" for _ in res)
    assert any(_[-1][0] == "::1" for _ in res)

    resolver.close()


@requires_network()
@pytest.mark.parametrize(
    "dns_url",
    [
        "doh+google://",
        "doh+cloudflare://",
        pytest.param(
            "doq://dns.nextdns.io/?timeout=1",
            marks=pytest.mark.xfail(
                os.environ.get("CI", None) is not None,
                reason="Github Action CI Unpredictable",
                strict=False,
            ),
        ),
        "dot://one.one.one.one",
        "dou://one.one.one.one",
    ],
)
def test_short_endurance_sprint(dns_url: str) -> None:
    resolver = ResolverDescription.from_url(dns_url).new()

    for host in [
        "www.google.com",
        "www.google.fr",
        "www.cloudflare.com",
        "youtube.com",
    ]:
        for addr_type in [socket.AF_UNSPEC, socket.AF_INET, socket.AF_INET6]:
            res = resolver.getaddrinfo(
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

    resolver.close()


@requires_network()
@pytest.mark.parametrize(
    "dns_url",
    [
        "doh+google://default?rfc8484=true",
        "doh+cloudflare://default?rfc8484=true",
        "doh://dns.nextdns.io/dns-query?rfc8484=true",
        "doh+adguard://",
    ],
)
def test_doh_rfc8484(dns_url: str) -> None:
    resolver = ResolverDescription.from_url(dns_url).new()

    for host in [
        "www.google.com",
        "www.google.fr",
        "www.cloudflare.com",
        "youtube.com",
    ]:
        for addr_type in [socket.AF_UNSPEC, socket.AF_INET, socket.AF_INET6]:
            res = resolver.getaddrinfo(
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

    resolver.close()


@requires_network()
@pytest.mark.parametrize(
    "dns_url",
    [
        "doh+google://",
        "doh+cloudflare://",
        pytest.param(
            "doq://dns.nextdns.io/?timeout=1",
            marks=pytest.mark.xfail(
                os.environ.get("CI", None) is not None,
                reason="Github Action CI Unpredictable",
                strict=False,
            ),
        ),
        "dot://one.one.one.one",
        "dou://one.one.one.one",
    ],
)
def test_thread_safe_resolver(dns_url: str) -> None:
    resolver = ResolverDescription.from_url(dns_url).new()

    def _run(
        target_name: str,
    ) -> list[
        tuple[
            AddressFamily,
            SocketKind,
            int,
            str,
            tuple[str, int] | tuple[str, int, int, int],
        ]
    ]:
        return resolver.getaddrinfo(
            target_name,
            443,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )

    with ThreadPoolExecutor(max_workers=5) as executor:
        f = []

        for name in [
            "www.google.com",
            "www.cloudflare.com",
            "youtube.com",
            "github.com",
            "api.github.com",
        ]:
            f.append(executor.submit(_run, name))

        for ff, idx in zip(f, range(0, len(f))):
            ff.result()

    resolver.close()


@requires_network()
def test_many_resolver_thread_safe() -> None:
    resolvers = [
        ResolverDescription.from_url("doh+google://").new(),
        ResolverDescription.from_url("doh+cloudflare://").new(),
        ResolverDescription.from_url("doh+adguard://").new(),
        ResolverDescription.from_url("dot+google://").new(),
        ResolverDescription.from_url("doh+google://").new(),
    ]

    resolver = ManyResolver(*resolvers)

    def _run(
        target_name: str,
    ) -> list[
        tuple[
            AddressFamily,
            SocketKind,
            int,
            str,
            tuple[str, int] | tuple[str, int, int, int],
        ]
    ]:
        return resolver.getaddrinfo(
            target_name,
            443,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )

    with ThreadPoolExecutor(max_workers=5) as executor:
        f = []

        for name in [
            "www.google.com",
            "www.cloudflare.com",
            "youtube.com",
            "github.com",
            "api.github.com",
            "gist.github.com",
        ]:
            f.append(executor.submit(_run, name))

        for ff, idx in zip(f, range(0, len(f))):
            ff.result()

    resolver.close()


@requires_network()
@pytest.mark.parametrize(
    "dns_url",
    [
        "doh+google://",
        "doh+cloudflare://",
        pytest.param(
            "doq://dns.nextdns.io/?timeout=1",
            marks=pytest.mark.xfail(
                os.environ.get("CI", None) is not None,
                reason="Github Action CI Unpredictable",
                strict=False,
            ),
        ),
        "dot://one.one.one.one",
        "dou://one.one.one.one",
    ],
)
def test_resolver_recycle(dns_url: str) -> None:
    resolver = ResolverDescription.from_url(dns_url).new()

    resolver.close()

    old_resolver, resolver = resolver, resolver.recycle()

    assert type(old_resolver) is type(resolver)

    assert resolver.protocol == old_resolver.protocol
    assert resolver.specifier == old_resolver.specifier
    assert resolver.implementation == old_resolver.implementation

    assert resolver.is_available()
    assert not old_resolver.is_available()

    resolver.close()

    assert not resolver.is_available()


@requires_network()
@pytest.mark.parametrize(
    "dns_url",
    [
        "doh+google://",
        "doh+cloudflare://",
        pytest.param(
            "doq://dns.nextdns.io/?timeout=1",
            marks=pytest.mark.xfail(
                os.environ.get("CI", None) is not None,
                reason="Github Action CI Unpredictable",
                strict=False,
            ),
        ),
        "dot://one.one.one.one",
        "dou://one.one.one.one",
    ],
)
def test_resolve_cannot_recycle_when_available(dns_url: str) -> None:
    resolver = ResolverDescription.from_url(dns_url).new()

    with pytest.raises(RuntimeError):
        resolver.recycle()

    resolver.close()


@requires_network()
@pytest.mark.parametrize(
    "dns_url",
    [
        "doh+google://",
        "doh+cloudflare://",
        pytest.param(
            "doq://dns.nextdns.io/?timeout=1",
            marks=pytest.mark.xfail(
                os.environ.get("CI", None) is not None,
                reason="Github Action CI Unpredictable",
                strict=False,
            ),
        ),
        "dot://one.one.one.one",
        "dou://one.one.one.one",
    ],
)
def test_ipv6_always_preferred(dns_url: str) -> None:
    """Our resolvers must place IPV6 address in the beginning of returned list."""
    resolver = ResolverDescription.from_url(dns_url).new()

    inet_classes = []

    res = resolver.getaddrinfo(
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

    resolver.close()


@requires_network()
@pytest.mark.parametrize(
    "dns_url",
    [
        "doh+google://",
        "doh+cloudflare://",
        pytest.param(
            "doq://dns.nextdns.io/?timeout=1",
            marks=pytest.mark.xfail(
                os.environ.get("CI", None) is not None,
                reason="Github Action CI Unpredictable",
                strict=False,
            ),
        ),
        "dot://one.one.one.one",
        "dou://one.one.one.one",
    ],
)
def test_dgram_upgrade(dns_url: str) -> None:
    """www.cloudflare.com records HTTPS exist, we know it. This verify that we are able to propose a DGRAM upgrade."""
    resolver = ResolverDescription.from_url(dns_url).new()

    sock_types = []

    res = resolver.getaddrinfo(
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

    resolver.close()


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
def test_in_memory_resolver(
    dns_url: str, hostname: str, expected_addr: str | None
) -> None:
    resolver = ResolverDescription.from_url(dns_url).new()

    assert resolver.have_constraints()

    if expected_addr is None:
        with pytest.raises(socket.gaierror):
            resolver.getaddrinfo(
                hostname,
                80,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
        return

    res = resolver.getaddrinfo(
        hostname,
        80,
        socket.AF_UNSPEC,
        socket.SOCK_STREAM,
    )

    assert any([_[-1][0] == expected_addr for _ in res])


@requires_network()
def test_doh_http11() -> None:
    """Ensure we can do DoH over HTTP/1.1 even if... that's absolutely not recommended!"""
    resolver = ResolverDescription.from_url(
        "doh+google://default/?disabled_svn=h2,h3"
    ).new()

    res = resolver.getaddrinfo(
        "www.cloudflare.com",
        80,
        socket.AF_UNSPEC,
        socket.SOCK_STREAM,
    )

    assert len(res)


@requires_network()
def test_doh_on_connection_callback() -> None:
    """Ensure we can inspect the resolver connection with a callback."""
    resolver_description = ResolverDescription.from_url("doh+google://")

    toggle_witness: bool = False

    def callback(conn_info: ConnectionInfo) -> None:
        nonlocal toggle_witness
        if conn_info:
            toggle_witness = True

    resolver_description["on_post_connection"] = callback

    resolver = resolver_description.new()

    res = resolver.getaddrinfo(
        "www.cloudflare.com",
        80,
        socket.AF_UNSPEC,
        socket.SOCK_STREAM,
    )

    assert len(res)
    assert toggle_witness


@pytest.mark.parametrize("dns_url", ["system://", "in-memory://", "null://"])
def test_not_closeable_recycle(dns_url: str) -> None:
    r = ResolverDescription.from_url(dns_url).new()

    r.close()

    assert r.is_available()

    rr = r.recycle()

    assert rr == r


def test_recycle_in_memory_with_mock() -> None:
    r = ResolverDescription.from_url(
        "in-memory://default/?hosts=localhost:8.8.8.8&hosts=local:1.1.1.1&maxsize=8"
    ).new()

    assert r.is_available()
    assert len(r._hosts) == 2  # type: ignore[attr-defined]
    assert "localhost" in r._hosts  # type: ignore[attr-defined]
    assert "local" in r._hosts  # type: ignore[attr-defined]
    assert r._maxsize == 8  # type: ignore[attr-defined]

    r.close()

    assert r.is_available()

    r.is_available = lambda: False  # type: ignore

    assert not r.is_available()

    rr = BaseResolver.recycle(r)

    assert rr.is_available()
    assert len(rr._hosts) == 2  # type: ignore[attr-defined]
    assert "localhost" in rr._hosts  # type: ignore[attr-defined]
    assert "local" in rr._hosts  # type: ignore[attr-defined]
    assert rr._maxsize == 8  # type: ignore[attr-defined]
