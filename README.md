<h1 align="center">
  <img src="https://github.com/jawah/urllib3.future/raw/main/docs/_static/logo.png" width="450px" alt="urllib3.future logo"/>
</h1>

<p align="center">
  <strong>Modern HTTP. Familiar urllib3 API. Broad deployment choice.</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/urllib3-future"><img alt="PyPI Version" src="https://img.shields.io/pypi/v/urllib3-future.svg?maxAge=86400" /></a>
  <a href="https://pypi.org/project/urllib3-future"><img alt="Python Versions" src="https://img.shields.io/pypi/pyversions/urllib3-future.svg?maxAge=86400" /></a>
  <a href="https://github.com/jawah/urllib3.future/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/jawah/urllib3.future/actions/workflows/ci.yml/badge.svg" /></a>
  <a href="https://github.com/jawah/urllib3.future/actions/workflows/integration.yml"><img alt="Downstream compatibility" src="https://github.com/jawah/urllib3.future/actions/workflows/integration.yml/badge.svg" /></a>
</p>

**urllib3.future** is an independently maintained, compatibility-first fork of
urllib3. It provides HTTP/1.1, HTTP/2, HTTP/3, synchronous and asynchronous
APIs, multiplexing, advanced DNS resolution, and multiple TLS backends while
preserving the established urllib3 programming model.

Existing urllib3 applications can adopt a modern transport stack without
migrating to a different HTTP client API:

```python
import urllib3

with urllib3.PoolManager() as pool:
    response = pool.request("GET", "https://httpbin.org/robots.txt")

print(response.status)   # 200
print(response.version)  # 11, 20, or 30
print(response.data)
```

Native asyncio APIs are available through the same package:

```python
import asyncio
import urllib3


async def main() -> None:
    async with urllib3.AsyncPoolManager() as pool:
        response = await pool.request(
            "GET",
            "https://httpbin.org/robots.txt",
        )

        print(response.status)
        print(response.version)
        print(await response.data)


asyncio.run(main())
```

## Why urllib3.future?

### Upgrade the transport, not the application

urllib3 sits beneath a large part of the Python HTTP ecosystem: directly,
beneath Requests, and inside cloud SDKs, database clients, automation tools,
and other libraries. Migrating every integration to a different client API can
be expensive and can fragment otherwise stable application code.

urllib3.future keeps the familiar API while replacing the transport with a
protocol-neutral implementation capable of HTTP/1.1, HTTP/2, and HTTP/3.

### Choice without fragmentation

The project does not require one HTTP generation, TLS implementation, resolver,
concurrency model, or runtime. Secure defaults are provided without turning
those defaults into artificial deployment limits.

- Async.
- Task safety.
- Thread safety.
- Happy Eyeballs.
- Connection pooling.
- Unopinionated about OpenSSL.
- Client-side SSL/TLS verification.
- Highly customizable DNS resolution.
- File uploads with multipart encoding.
- DNS over UDP, TLS, QUIC, or HTTPS. DNSSEC protected.
- Helpers for retrying requests and dealing with HTTP redirects.
- Automatic Keep-Alive for HTTP/1.1, HTTP/2, and HTTP/3.
- Support for gzip, deflate, brotli, and zstd encoding.
- Support for Python/PyPy 3.7+, no compromise.
- Automatic Connection Upgrade / Downgrade.
- Early (Informational) Responses / Hints.
- HTTP/1.1, HTTP/2 and HTTP/3 support.
- WebSocket over HTTP/2+ (RFC8441).
- Proxy support for HTTP and SOCKS.
- Detailed connection inspection.
- Post-Quantum Security & ECH.
- HTTP/2 with prior knowledge.
- Support for free-threaded.
- Server Side Event (SSE).
- Multiplexed connection.
- Mirrored Sync & Async.
- Trailer Headers.
- WebSocket.
- WASI.

The
[documentation](https://urllib3future.readthedocs.io/) describes the
requirements and limitations of each feature.

This philosophy is inspired by curl: broad protocol and platform capability,
stable interfaces, secure defaults, and user choice where deployment
requirements differ.

### Compatibility is a release requirement

Compatibility is continuously measured against inherited urllib3 behavior,
protocol integration tests, and the actual test suites of major downstream
projects.

When a reasonable downstream pattern breaks, we investigate it as a
compatibility defect rather than dismissing it solely because it touches an
internal API.

## Tested beyond the happy path

The CI matrix is intentionally broad because compatibility and deployment
choice are core features of the project.

| Dimension  | Continuous coverage                                                                             |
|------------|-------------------------------------------------------------------------------------------------|
| Python     | CPython, PyPy, supported historical versions, current versions, free-threaded builds            |
| TLS        | Modern OpenSSL, older OpenSSL, LibreSSL, stdlib TLS, Rustls with the AWS-LC provider, BoringSSL |
| Protocols  | HTTP/1.1, HTTP/2, HTTP/3, prior knowledge, upgrade, downgrade, multiplexing                     |
| APIs       | Synchronous and asynchronous implementations                                                    |
| Networking | IPv4, IPv6, Happy Eyeballs, HTTP proxies, HTTPS proxies, SOCKS, TLS-in-TLS                      |
| Resolvers  | System, DoH, DoT, DoQ, DoU, in-memory, resolver composition                                     |
| Runtime    | Linux, Windows, macOS, containers, WASI Preview 1, Preview 2, and Preview 3                     |

Protocol integration tests use Traefik and go-httpbin so the client is exercised
against independent, non-Python server implementations.

Downstream CI runs the downstream projects' own test orchestration. Current
coverage includes:

| Project            | Why it matters                                                                     |
|--------------------|------------------------------------------------------------------------------------|
| Requests           | The most widely used urllib3 consumer and a major public API compatibility surface |
| Niquests           | Primary consumer of urllib3.future's extended sync and async capabilities          |
| botocore           | Deep integration with urllib3 internals and the foundation of the AWS Python SDK   |
| boto3              | High-level AWS SDK behavior layered over botocore                                  |
| Sphinx             | Widely used documentation tooling and HTTP link checking                           |
| docker-py          | Streaming, connection pooling, and Docker transport behavior                       |
| clickhouse-connect | Database transport, streaming, and chunked-transfer behavior                       |

These checks provide measurable coverage of the tested behavior. No finite test
matrix can prove compatibility with every application or undocumented
integration, so applications should still run their own representative tests.

- [Main CI](https://github.com/jawah/urllib3.future/actions/workflows/ci.yml)
- [Downstream integration CI](https://github.com/jawah/urllib3.future/actions/workflows/integration.yml)
- [CodeQL](https://github.com/jawah/urllib3.future/actions/workflows/codeql.yml)
- [OpenSSF Scorecard](https://github.com/jawah/urllib3.future/actions/workflows/scorecards.yml)
- [Release history](https://github.com/jawah/urllib3.future/releases)

## Installation modes

urllib3.future supports two installation models.

| Mode         | Import                  | Effect                                                             | Appropriate when                                                                            |
|--------------|-------------------------|--------------------------------------------------------------------|---------------------------------------------------------------------------------------------|
| Drop-in      | `import urllib3`        | urllib3.future becomes the environment-wide urllib3 implementation | Applications, managed runtimes, and libraries intentionally standardizing on urllib3.future |
| Cohabitation | `import urllib3_future` | Upstream urllib3 retains the `urllib3` namespace                   | Applications requiring strict namespace separation                                          |

### Drop-in mode

Install the published wheel:

```bash
python -m pip install urllib3-future
```

Then use the established import:

```python
import urllib3
```

This is an environment-wide transport selection. Packages in the same Python
environment that import `urllib3` will use urllib3.future too.

### Cohabitation mode

To keep upstream urllib3 and urllib3.future under separate namespaces, follow
the package-manager-specific
[cohabitation instructions](https://niquests.readthedocs.io/en/latest/community/faq.html#cohabitation).

The pip form is:

```bash
URLLIB3_NO_OVERRIDE=1 \
python -m pip install urllib3-future --no-binary urllib3-future
```

Then import the fork through its separate namespace:

```python
import urllib3_future
```

`URLLIB3_NO_OVERRIDE` is evaluated while building the package. It is not a
runtime switch and does not alter an already-built wheel.

OS and distribution package maintainers should use cohabitation mode when the
system package manager is responsible for namespace ownership.

## Depending on urllib3.future from a library

A PyPI library can depend on urllib3.future when its supported transport
baseline requires capabilities provided by this implementation.

Because the default wheel selects the urllib3 implementation for the Python
environment, treat the dependency as an intentional platform decision rather
than an invisible optimization.

We recommend:

- Declaring the compatible major-version range: `urllib3-future>2,<3`.
- Running the library's full test suite with common urllib3 consumers present.
- Mentioning the transport selection in release notes.
- Reporting compatibility regressions to this project; they are treated as release blockers.

Libraries that require only the standard urllib3 API may leave implementation
selection to the application. Libraries that rely on HTTP/2, HTTP/3, async
APIs, multiplexing, advanced DNS, WASI, or alternative TLS support can declare
urllib3.future as their intended runtime.

## Namespace selection

Python packaging has no standard mechanism for one distribution to declare
that it is a compatible replacement for another distribution while satisfying
existing imports of the original package.

This matters beyond import convenience. Requests extensions and other urllib3
consumers exchange urllib3 exceptions, response objects, connection classes,
and type annotations. A separate namespace preserves isolation, but objects
from `urllib3` and `urllib3_future` no longer share identity. Existing packages
that import `urllib3` directly would also continue using the upstream transport.

The default wheel includes both `urllib3` and `urllib3_future`. It also includes
a small, inspectable `.pth` startup script. If files from upstream urllib3 and
urllib3.future are mixed in the same `urllib3` package directory, the script
recreates that directory from the authoritative `urllib3_future` copy before
application imports begin.

The mechanism:

- Does not patch `sys.modules`.
- Does not depend on application import order.
- Leaves a physical `urllib3` package visible to Python, IDEs, type checkers, packagers, and static analysis tools.
- Becomes a no-op once the environment is consistent.
- Is excluded entirely from cohabitation builds.

The implementation is available in
[`urllib3_future.pth`](https://github.com/jawah/urllib3.future/blob/main/urllib3_future.pth)
and
[`hatch_build.py`](https://github.com/jawah/urllib3.future/blob/main/hatch_build.py).

This behavior is intentional, environment-wide, and reversible through the
documented cohabitation installation mode.

## Compatibility policy

The compatibility target is practical drop-in operation for existing urllib3
consumers.

The project preserves urllib3's public top-level import surface and selected
private behavior required by major downstream packages. Applicable upstream
bug fixes and security patches are reviewed and incorporated.

Compatibility regressions are priority defects. If an existing urllib3 pattern
does not work with urllib3.future, please open an issue with a minimal
reproducer and the affected downstream package.

Fork releases use the `MAJOR.MINOR.9xx` version form. The `9xx` patch range
distinguishes urllib3.future releases from upstream urllib3 releases.

## Frequently asked questions

### Is this a fork?

Yes. urllib3.future is an independently maintained fork of urllib3 with a
different transport architecture and feature scope.

The fork preserves the familiar urllib3 API while adding capabilities that
require deeper changes than an extension module can provide. Issues and support
requests for urllib3.future should be reported in this repository.

### Why a fork instead of a new HTTP client?

A new client API would require applications, Requests integrations, SDKs, and
other urllib3 consumers to migrate individually.

The fork keeps the established API and ecosystem contract while allowing the
transport implementation to evolve. Applications can modernize their HTTP
stack without replacing every integration layered above it.

### Why not contribute everything upstream?

We explored the architecture upstream first. The projects chose different
technical scopes and development strategies.

urllib3.future prioritizes a unified protocol-neutral backend, transparent
HTTP/1.1, HTTP/2, and HTTP/3 negotiation, mirrored async APIs, broad TLS backend
choice, and support for older and alternative runtime environments.

Maintaining that scope independently allows each project to evolve according
to its own priorities.

### Why is namespace replacement the default?

The compatibility objective is that existing code using `import urllib3`
continues to work without adapters or runtime injection.

Python packaging has no standard concept of one distribution replacing
another. The default wheel therefore makes namespace selection before
application imports begin. This choice applies to the environment, not one
individual dependency.

Cohabitation mode is available when strict namespace separation is preferred.

### Why not use an optional extra?

An extra selected by one transitive dependency would still affect every package
in the environment while making the environment-wide effect appear local to
that dependency.

urllib3.future instead provides two documented modes with different outcomes:
drop-in replacement and strict cohabitation.

### Why not inject at runtime?

Runtime injection through `sys.modules` makes behavior depend on import order
and hides namespace selection inside application code.

The installation-time strategy runs before application imports and leaves a
physical package visible to static and runtime tooling.

### Is urllib3.future fully compatible with urllib3?

Compatibility is a primary project objective, but no finite test suite can
guarantee compatibility with every application, dependency combination, or
private integration.

The project maintains urllib3's public import surface and continuously tests
widely used downstream projects. Compatibility failures are investigated as
project defects when preserving the behavior is technically and securely
reasonable.

### Is urllib3.future production-ready?

The project is designed and maintained for production use. It is exercised
through a broad Python, TLS, protocol, platform, and downstream compatibility
matrix.

Teams should still pin a tested version, run their own integration and load
tests, and exercise the transport paths they rely on.

### Is it faster than urllib3?

It can be substantially faster for workloads that benefit from connection
reuse, concurrency, and HTTP/2 or HTTP/3 multiplexing. A serial HTTP/1.1
workload may show no improvement.

Performance is workload-specific. Use reproducible benchmarks and
application-specific testing rather than assuming a universal speedup.

### What happens when compatibility breaks?

Compatibility regressions are priority defects. Please report the smallest
reproducer and the affected downstream package.

The project regularly accepts compatibility fixes even when a downstream uses
an urllib3 internal, provided the behavior is reasonable and preserving it does
not compromise protocol correctness or security.

### Can upstream urllib3 and urllib3.future coexist?

Yes. Cohabitation mode leaves upstream urllib3 behind `import urllib3` and
provides the fork through `import urllib3_future`.

See the
[cohabitation instructions](https://niquests.readthedocs.io/en/latest/community/faq.html#cohabitation)
for pip, Poetry, PDM, and uv.

### What are the known platform boundaries?

Emscripten and Pyodide are not currently supported by urllib3.future but WASI is.
Use upstream urllib3 on those platforms, optionally through cohabitation mode.

HTTP/3 availability depends on a supported platform and the semi-optional qh3
dependency. ECH and post-quantum capabilities depend on the selected TLS
backend and its configuration.

### Is there precedent or prior art for in-place replacement?

Yes. A Python distribution name and an import namespace are not the same
concept, and Python does not grant a distribution exclusive ownership of an
import name. Several established projects have provided an existing import
interface through an alternative distribution.

Notable examples include:

- [Pillow](https://python-pillow.org/) succeeded PIL while preserving the
  established `PIL` import namespace.
- [PyCryptodome](https://www.pycryptodome.org/) can act as a drop-in replacement
  for PyCrypto through the `Crypto` namespace, while PyCryptodomex provides an
  isolated `Cryptodome` namespace.
- [mysqlclient](https://github.com/PyMySQL/mysqlclient) continued the
  MySQL-python interface through the existing `MySQLdb` namespace.
- [pillow-simd](https://github.com/uploadcare/pillow-simd) provides an
  alternative Pillow implementation through the same `PIL` namespace.
- [setuptools](https://github.com/pypa/setuptools) uses
  `distutils-precedence.pth` and `_distutils_hack` to select its bundled
  implementation of `distutils` before application imports.

Related replacement mechanisms are also common outside PyPI. Operating-system
package managers support concepts such as `Provides`, `Conflicts`, and
`Replaces`, and distributors routinely patch or redirect implementations while
preserving their established interfaces. For example, some distributions make
`certifi` use the system certificate store without changing the public
`certifi` import API.

These precedents are not identical to urllib3.future. Most replaced an
unmaintained project, provided alternate builds of the same project, or relied
on an explicit operating-system package manager. urllib3.future is unusual
because it independently implements the interface of a widely deployed and
actively maintained project, while supporting both drop-in replacement and
strict namespace separation.

The novelty is therefore not the idea that an interface can have more than one
implementation. The difficult part is sustaining compatibility with urllib3's
documented API, historical behavior, downstream integrations, object identity,
and frequently used internal interfaces while continuing to evolve the
transport architecture.

Anyone may implement the urllib3 interface in principle. In practice, doing so
requires accepting its accumulated compatibility constraints as release
requirements. urllib3.future treats that obligation as measurable engineering
work through the inherited urllib3 suite, downstream project suites, and
independent protocol integration tests.

## Known compatibility differences

The following intentional differences may affect applications that rely on representation details inherited from HTTP/1
or on urllib3's historical use of the standard library. They do not change the underlying HTTP semantics, but applications
and integrations relying on those implementation details may require adjustments.

### Header-name casing

HTTP field names are case-insensitive in requests and responses. Upstream `urllib3` provides case-insensitive access
through `HTTPHeaderDict`, regardless of the casing used by the application or received from the server.

With HTTP/1, field names can nevertheless retain their original wire casing. This has allowed some client applications
to accidentally depend on presentation details such as `Content-Type` instead of treating it as equivalent to
`content-type`.

Some server applications, middleware, gateways, or custom HTTP parsers may have the same defect and incorrectly require
a specific spelling, such as `Authorization` rather than `authorization`. When urllib3.future sends a differently cased
but semantically equivalent field name, such a server may reject the request, fail to locate the header, or return an
internal error such as HTTP 500.

This server-side behavior is rare and violates the HTTP specifications, which define field names as case-insensitive.
Such implementations are also inherently incompatible with HTTP/2 and HTTP/3, where field names are required to be
lowercase.

urllib3.future normalizes outgoing field names consistently across HTTP/1, HTTP/2, and HTTP/3 instead of exposing
protocol-dependent casing. Incoming HTTP/1 field names retain the spelling sent by the server, while incoming HTTP/2 and
HTTP/3 field names are lowercase as required by those protocols. The lowercase requirement is not merely a consequence
of HPACK, QPACK, or binary framing.

**Remediation:** Treat header names as case-insensitive on both the client and server. Do not compare or assert their
presentation casing. Server applications and middleware should normalize field names before matching them. If a
non-compliant server cannot be corrected, use an HTTP client or transport that preserves its HTTP/1 casing requirement.

### Reason phrases

HTTP/1 responses may include a server-defined reason phrase. Upstream `urllib3` historically exposed that value through
`response.reason`, including its original wording and casing.

For example, an HTTP/1.1 server may return:

```http
HTTP/1.1 200 SUCCESS
```

and upstream `urllib3` may expose:

```python
response.reason == "SUCCESS"
```

HTTP/2 and HTTP/3 carry only the numeric status code and do not provide a reason phrase. To preserve a stable
`response.reason` API across all supported HTTP versions, urllib3.future derives the canonical phrase from the numeric
status code. Unknown status codes use `"Unknown"`.

As a result, a custom HTTP/1 reason phrase is not preserved:

```python
response.status == 200
response.reason == "OK"
```

This normalization is necessary to provide consistent behavior independently of the negotiated HTTP version.

**Remediation:** Treat the numeric status code as authoritative. Treat `response.reason` as an informational,
implementation-derived value, and do not depend on server-specific wording or casing.

### `http.client` observability

Upstream `urllib3`'s HTTP/1 implementation relies on the standard library's `http.client`. Some integrations consequently
instrument, monkeypatch, or observe `http.client` directly instead of instrumenting `urllib3`.

urllib3.future uses a protocol-independent transport architecture and does not rely on `http.client`, including for
HTTP/1. Instrumentation attached only to `http.client` therefore does not observe urllib3.future traffic.

This is also an unavoidable boundary for a multi-protocol urllib3 implementation. Even if an implementation retains
`http.client` for HTTP/1, HTTP/2 and HTTP/3 traffic cannot pass through the same inheritance path. An integration observing
`http.client` would then behave inconsistently depending on protocol negotiation: some requests would be visible and
others would not.

**Remediation:** Instrument `urllib3` at its own API or transport boundaries. Do not assume that observing or monkeypatching
`http.client` provides complete coverage for a client capable of negotiating multiple HTTP versions.

## Release integrity

Releases are published through PyPI trusted publishing and include provenance
generated by the release workflow.

The project uses:

- Commit-pinned GitHub Actions.
- CodeQL analysis.
- OpenSSF Scorecard checks.
- PyPI attestations.
- SLSA provenance.
- Public release artifacts and changelog history.

These controls make the build and publication path auditable. They complement,
but do not replace, application-specific compatibility and security testing.

urllib3-future is also included in Google's
[Assured Open Source Software catalog](https://cloud.google.com/security-command-center/docs/aoss-supported-packages-premium).
Assured OSS provides Google-built artifacts with provenance and security
metadata; its inclusion is an additional supply-chain option, not a substitute
for evaluating the PyPI release or this project's design decisions.

## Security disclosures

To report a security vulnerability, use the
[Tidelift security contact](https://tidelift.com/security). Tidelift will
coordinate the fix and disclosure with the maintainers.

## Documentation

Full usage and API documentation is available at
[urllib3future.readthedocs.io](https://urllib3future.readthedocs.io/).

## Contributing

Contributions are welcome. The specialized CI environments are not expected to
be reproduced by every contributor; run the relevant local tests and linting,
then let CI exercise the broader matrix.

```bash
python -m pip install nox
nox -s test-3.14
nox -s lint
```

Replace `3.14` with an available supported interpreter version.

See the contributor documentation for protocol, downstream, legacy TLS, and
container-based test commands.

## Sponsorship

urllib3.future receives recurring maintenance funding and sponsorship. Funding
supports compatibility work, security updates, CI infrastructure, protocol
maintenance, and downstream testing.

Funding does not replace community review or imply a support SLA. If your
company depends on urllib3.future, consider sponsoring its continued
maintenance.

## A short history

urllib3.future began with a practical question:

> Can Python applications gain HTTP/2, HTTP/3, and asynchronous capabilities
> without migrating the ecosystem away from urllib3?

In May 2023, an
[experimental contribution to urllib3](https://github.com/urllib3/urllib3/pull/3030)
demonstrated a unified HTTP/1.1, HTTP/2, and HTTP/3 backend outside
`http.client`. The experiment reached full coverage of its prototype and
identified the compatibility, streaming, proxy, TLS, and integration work
required for a maintained implementation.

The projects ultimately chose different architectural scopes and development
strategies. Upstream urllib3 continued its own roadmap, while the unified
protocol backend continued independently and became urllib3.future. Later in
2023, upstream opened a
[discussion about collaboration](https://github.com/jawah/urllib3.future/issues/46);
the projects nevertheless continued as separately maintained implementations
with different priorities.

Since then, urllib3.future has expanded beyond the original protocol prototype:
it gained mirrored synchronous and asynchronous APIs, HTTP/3, multiplexing,
advanced resolvers, multiple TLS backends, broad downstream compatibility, and
WASI support.

urllib3.future remains grateful for the foundation built by urllib3's
maintainers and contributors. Applicable upstream fixes and security patches
continue to be reviewed and incorporated. The two projects serve different
scopes, and users are free to choose the implementation that best matches their
requirements.
