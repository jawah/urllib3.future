<h1 align="center">
<img src="https://github.com/jawah/urllib3.future/raw/main/docs/_static/logo.png" width="450px" alt="urllib3.future logo"/>
</h1>

<p align="center">
  <a href="https://pypi.org/project/urllib3-future"><img alt="PyPI Version" src="https://img.shields.io/pypi/v/urllib3-future.svg?maxAge=86400" /></a>
  <a href="https://pypi.org/project/urllib3-future"><img alt="Python Versions" src="https://img.shields.io/pypi/pyversions/urllib3-future.svg?maxAge=86400" /></a>
  <br><small>urllib3.future is as BoringSSL is to OpenSSL but to urllib3 (but with available support!)</small>
</p>

⚡ urllib3.future is a powerful, *user-friendly* HTTP client for Python.<br>
⚡ urllib3.future goes beyond supported features while remaining compatible.<br>
⚡ urllib3.future brings many critical features that are missing from both the Python standard libraries **and urllib3**:

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
- Amazingly Fast.
- WebSocket.

urllib3.future is powerful and easy to use:

```python
>>> import urllib3
>>> pm = urllib3.PoolManager()
>>> resp = pm.request("GET", "https://httpbin.org/robots.txt")
>>> resp.status
200
>>> resp.data
b"User-agent: *\nDisallow: /deny\n"
>>> resp.version
20
```

or using asyncio!

```python
import asyncio
import urllib3

async def main() -> None:
    async with urllib3.AsyncPoolManager() as pm:
        resp = await pm.request("GET", "https://httpbin.org/robots.txt")
        print(resp.status)  # 200
        body = await resp.data
        print(body)  # # b"User-agent: *\nDisallow: /deny\n"
        print(resp.version)  # 20

asyncio.run(main())
```

## Installing

urllib3.future can be installed with [pip](https://pip.pypa.io):

```bash
python -m pip install urllib3.future
```

You either do

```python
import urllib3
```

Or...

```bash
URLLIB3_NO_OVERRIDE=1 python -m pip install urllib3.future --no-binary urllib3.future
```

```python
import urllib3_future
```

## Background

In 2018, a Requests 3 was announced with plans for async and HTTP/2 support, backed by a ~$30k fundraiser.
The work never materialized. Requests entered a feature freeze, and urllib3 -- its foundation --
showed no signs of pursuing HTTP/2 support on its own.

A [contribution bringing HTTP/2 support to urllib3](https://github.com/urllib3/urllib3/pull/3030)
was proposed but abandoned after months without review. Shortly after [they discovered urllib3-future](https://github.com/jawah/urllib3.future/issues/46), urllib3 launched [its fundraiser](https://sethmlarson.dev/urllib3-is-fundraising-for-http2-support) seeking ~$40-50k USD
for HTTP/2 support alone. The work around this complex milestone, since, is very stale.

urllib3-future starts on a simple premise: HTTP/2 has been a finalized standard for well over a decade, and we should
be able to leverage it without asking the massive community to shift toward yet another http client. It's exhausting
for developers to shift around fundamental libraries.

## Notes / Frequently Asked Questions

- **It's a fork**

urllib3.future is a backward-compatible fork that extends urllib3 with HTTP/2 and HTTP/3 support.
When installed, it takes precedence over urllib3 to provide a consistent environment.
The semver will always be like _MAJOR.MINOR.9PP_ like 2.0.941, the patch node is always greater or equal to 900.

Support for bugs or improvements is served in this repository. We regularly sync this fork
with the main branch of urllib3/urllib3 against bugfixes and security patches if applicable.

This package is a drop-in replacement, fully compatible with its predecessor. Found anything not compatible? We'll fix it.

- **Why fork urllib3 instead of contributing upstream?**

We built on the considerable work poured into urllib3 rather than starting from scratch.
We attempted to participate in urllib3 development but found ourselves in disagreement on the path forward.
This happens regularly in open source, even on the largest projects (e.g. OpenSSL vs BoringSSL, MySQL vs MariaDB).

- **Why does urllib3-future take precedence over urllib3?**

Python packaging has no native concept of one package replacing another.
When both are present (e.g. via transitive dependencies), package managers may install them concurrently,
leaving the `urllib3` namespace in an inconsistent state.
urllib3-future resolves this by ensuring its own code is what sits behind `import urllib3`.

Backward compatibility is treated as a top priority. We continuously test this fork against the most
widely used packages that depend on urllib3, including those that rely on urllib3 internals.

Most users are not fully aware of their transitive dependencies -- urllib3 is typically pulled in
automatically regardless of your preferences. urllib3-future ensures that when it is present, the
environment is deterministic.

- **Why an in-place fork rather than a separate package?**

We evaluated several approaches. An in-place fork carries real constraints, but the alternatives
were not viable given the ecosystem's structure:

> **A)** Some major companies may not be able to modify production code but can change/swap dependencies.
>
> **B)** urllib3-future's main purpose is to fuel Niquests, which is itself a drop-in replacement for Requests.
> More than 100 commonly used packages plug into Requests, but their code invokes urllib3 directly.
> We cannot fork those 100+ projects to patch urllib3 usage - it's impossible given our means.
> Requests trapped us, and there should be a way to escape the "migrate to another HTTP client" cycle
> that reinvents basic functionality.
>
> **C)** We don't have to reinvent the wheel.
>
> **D)** Some of our partners noticed that HTTP/1 is being disabled by some web services in favor of HTTP/2+.
> This fork can unblock them at almost zero cost.

- **Why not use an opt-in extra or a public inject function instead?**

These alternatives were considered and fail for the same fundamental reason:

> **"Make it an opt-in extra"** (e.g. `pip install urllib3-future[override]`): Once any single package
> in your dependency tree activates the extra, every other package in the environment is affected.
> The person who chose the extra is rarely the person who is surprised by the result. It creates
> the same outcome as the current approach, but with a false sense of user consent.
>
> **"Expose a public `inject_into_urllib3()` function"**: If any library calls this function at import
> time, the effect propagates to every other library in the process. This is equivalent to a
> `sys.modules` hack that silently redirects imports -- the same end result, but hidden inside
> application code instead of being visible in the packaging layer. It also introduces
> unpredictable behavior depending on import order.
>
> Both approaches converge to the same outcome as the current mechanism, but add indirection
> that makes the behavior harder to audit and reason about. The `.pth` approach is at least
> explicit: it lives in a single inspectable file in site-packages, operates before any user
> code runs, and is deterministic regardless of import order.
>
> There is also a practical paradox: the user who objects to the override most likely wants
> `import urllib3` to work exactly as before -- no extra steps, no adapter code. But the
> opt-out path (keeping both packages side by side) is precisely the one that breaks that
> expectation, since downstream packages that import urllib3 directly will not see
> urllib3-future's improvements. The default behavior is the one that gives the seamless,
> zero-friction experience most users actually want.

- **What do I gain from this?**

1. It is faster than its counterpart, we measured gain up to 2X faster in a multithreaded environment using a http2 endpoint.
2. It works well with gevent / does not conflict. We do not use the standard queue class from stdlib as it does not fit http2+ constraints.
3. Leveraging recent protocols like http2 and http3 transparently. Code and behaviors does not change one bit.
4. You do not depend on the standard library to emit http/1 requests, and that is actually a good news. http.client
  has numerous known flaws but cannot be fixed as we speak. (e.g. urllib3 is based on http.client)
5. There a tons of other improvement you may leverage, but for that you will need to migrate to Niquests or update your code
  to enable specific capabilities, like but not limited to: "DNS over QUIC, HTTP" / "Happy Eyeballs" / "Native Asyncio" / "Advanced Multiplexing".
6. Non-blocking IO with concurrent streams/requests. And yes, transparently.
7. It relaxes some constraints established by upstream in their version 2, thus making it easier to upgrade from version 1.

- **Is this funded?**

Yes! We have some funds coming in regularly to ensure its sustainability.

- **How do I keep urllib3 and urllib3-future side by side?**

You can install both packages independently. For example, to install Niquests
while keeping the original urllib3 untouched:

<details>
  <summary>👆 <b>pip</b></summary>

```shell
URLLIB3_NO_OVERRIDE=1 pip install niquests --no-binary urllib3-future
```

</details>

<details>
  <summary>👆 <b>Poetry</b></summary>

```shell
export URLLIB3_NO_OVERRIDE=1
poetry config --local installer.no-binary urllib3-future
poetry add niquests
```

or in a one-liner shortcut:

```shell
URLLIB3_NO_OVERRIDE=1 POETRY_INSTALLER_NO_BINARY=urllib3-future poetry add niquests
```

</details>

<details>
  <summary>👆 <b>PDM</b></summary>

```shell
URLLIB3_NO_OVERRIDE=1 PDM_NO_BINARY=urllib3-future pdm add niquests
```

or with a persistent configuration (via pyproject.toml):

```toml
[tool.pdm.resolution]
no-binary = "urllib3-future"
```

then:

```shell
export URLLIB3_NO_OVERRIDE=1
pdm add niquests
```
</details>

<details>
  <summary>👆 <b>UV</b></summary>

Add to your pyproject.toml:

```toml
[tool.uv]
no-binary-package = ["urllib3-future"]
```

then:

```shell
export URLLIB3_NO_OVERRIDE=1
uv add niquests  # sync / pip / ...
```
</details>

This applies to any package that transitively depends on urllib3-future.
It enforces strict separation between urllib3 and urllib3-future.
Note that without the in-place upgrade, some downstream packages that import urllib3 directly
will not benefit from urllib3-future's improvements.

- **How is compatibility ensured?**

Every change goes through multiple layers of verification before it can land:

**1. Upstream test suite.** We maintain the full urllib3 test suite and enforce it in CI.
Every test that passes against upstream urllib3 must also pass against urllib3-future.
This is the baseline that guarantees API and behavioral parity.

**2. Downstream integration tests.** On every push to `main` and every pull request, we
automatically clone and run the test suites of major projects that depend on urllib3:

| Project                | Why it matters                                                                                 |
|------------------------|------------------------------------------------------------------------------------------------|
| **Requests**           | The most widely used HTTP client in Python; exercises urllib3's public API surface extensively |
| **Niquests**           | Drop-in replacement for Requests, exercises urllib3-future's extended capabilities             |
| **botocore**           | The foundation of AWS SDK for Python; deeply coupled to urllib3 internals                      |
| **boto3**              | AWS SDK high-level API; built on top of botocore                                               |
| **Sphinx**             | Documentation generator used across the ecosystem; uses urllib3 for link checking              |
| **docker-py**          | Official Docker SDK for Python; exercises connection pooling and streaming                     |
| **clickhouse-connect** | ClickHouse database client; exercises chunked transfer and streaming edge cases                |

These are not toy tests -- they are the actual upstream test suites of each project, run
against the latest `main` of both urllib3-future and the downstream project. If any of
them breaks, the CI pipeline fails and the change does not merge.

**3. Priority policy.** Compatibility issues affecting downstream packages are treated
as top-priority bugs. A regression that breaks a library using urllib3 through its
public API is treated with the same urgency as a security issue.

- **Can I contribute?**

Yes! Contributing to this project is a rewarding challenge due to the breadth of constraints:
Python 3.7+, OpenSSL <1.1.1,>1, LibreSSL, downstream compatibility, API parity with urllib3, and more.

If you like a good challenge, then this project will definitely suit you.

Make sure everything passes before submitting a PR, unless you need guidance on a specific topic.

After applying your patch, run (Unix, Linux):

```shell
./ci/run_legacy_openssl.sh
./ci/run_legacy_libressl.sh
./ci/run_dockerized.sh
nox -s test-3.11
```

replace the `3.11` part in `test-3.11` by your interpreter version.

If the tests all passes, then it is a firm good start.

Complete them with:

```shell
nox -s downstream_requests
nox -s downstream_niquests
nox -s downstream_boto3
nox -s downstream_sphinx
```

Finally make sure to fix any lint errors:

```shell
nox -s lint
```

- **For OS package maintainers**

When packaging for a distribution registry, set `URLLIB3_NO_OVERRIDE=1` during the build
(e.g. `URLLIB3_NO_OVERRIDE=1 python -m build`). This prevents the in-place upgrade behavior,
which is appropriate for environments where the OS package manager controls package namespaces.

## Compatibility with downstream

Just adding `urllib3-future` in your dependency is enough.

e.g. I want `requests` to be use this package.

```shell
python -m pip install requests
python -m pip install urllib3.future
```

or just in your dependencies

```
[project]
name = "my-upgraded-project"
version = "0.1.0"
description = "Add your description here"
requires-python = ">=3.13"
dependencies = [
    "requests",
    "urllib3-future",
]
```

Nowadays, we suggest using the package [**Niquests**](https://github.com/jawah/niquests) as a drop-in replacement for **Requests**.
It leverages urllib3.future capabilities appropriately.

## Testing

To ensure that we serve HTTP/1.1, HTTP/2 and HTTP/3 correctly we use containers
that simulate a real-world server that is not made with Python.

Although it is not made mandatory to run the test suite, it is strongly recommended.

You should have docker installed and the compose plugin available. The rest will be handled automatically.

```shell
python -m pip install nox
nox -s test-3.11
```

The nox script will attempt to start a Traefik server along with a httpbin instance.
Both Traefik and httpbin are written in golang.

You may prevent the containers from starting by passing the following environment variable:

```shell
TRAEFIK_HTTPBIN_ENABLE=false nox -s test-3.11
```

## Documentation

urllib3.future has usage and reference documentation at [urllib3future.readthedocs.io](https://urllib3future.readthedocs.io).

## Contributing

urllib3.future happily accepts contributions.

## Security Disclosures

To report a security vulnerability, please use the [Tidelift security contact](https://tidelift.com/security).
Tidelift will coordinate the fix and disclosure with maintainers.

## Sponsorship

If your company benefits from this library, please consider sponsoring its
development.
