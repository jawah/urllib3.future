<h1 align="center">
<img src="https://github.com/jawah/urllib3.future/raw/main/docs/_static/logo.png" width="450px" alt="urllib3.future logo"/>
</h1>

<p align="center">
  <a href="https://pypi.org/project/urllib3-future"><img alt="PyPI Version" src="https://img.shields.io/pypi/v/urllib3-future.svg?maxAge=86400" /></a>
  <a href="https://pypi.org/project/urllib3-future"><img alt="Python Versions" src="https://img.shields.io/pypi/pyversions/urllib3-future.svg?maxAge=86400" /></a>
  <br><small>urllib3.future is as BoringSSL is to OpenSSL but to urllib3 (except support is available!)</small>
  <br><small>✨🍰 Enjoy HTTP like its 2024 🍰✨</small>
  <br><small>💰 Promotional offer, get everything and more for <del>40k</del> <b>0</b>$!</small>
  <br><small>Wondering why and how this fork exist? Why urllib3 does not merge this, even partially? <a href="https://medium.com/@ahmed.tahri/revived-the-promise-made-six-years-ago-for-requests-3-37b440e6a064">Take a peek at this article!</a></small>
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
- Post-Quantum Security with QUIC.
- Detailed connection inspection.
- HTTP/2 with prior knowledge.
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
$ python -m pip install urllib3.future
```

You either do 

```python
import urllib3
```

Or...

```python
import urllib3_future
```

Or... upgrade any of your containers with...

```dockerfile
FROM python:3.12

# ... your installation ...
RUN pip install .
# then! (after every other pip call)
RUN pip install urllib3-future
```

Doing `import urllib3_future` is the safest option if you start a project from scratch for you as there is a significant number of projects that
require `urllib3`.

## Notes / Frequently Asked Questions

- **It's a fork**

⚠️ Installing urllib3.future shadows the actual urllib3 package (_depending on installation order_). 
The semver will always be like _MAJOR.MINOR.9PP_ like 2.0.941, the patch node is always greater or equal to 900.

Support for bugs or improvements is served in this repository. We regularly sync this fork
with the main branch of urllib3/urllib3 against bugfixes and security patches if applicable.

- **Why replacing urllib3 when it is maintained?**

Progress does not necessarily mean to be a revisionist, first we need to embrace
what was graciously made by our predecessors. So much knowledge has been poured into this that
we must just extend it.

We attempted to participate in urllib3 development only to find that we were in disagreement on how
to proceed. It happens all the time, even on the biggest projects out there (e.g. OpenSSL vs BoringSSL or NSS or LibreSSL...))

- **OK, but I got there because I saw that urllib3 was replaced in my environment!**

Since Forks are allowed (fortunately for us); It how package manager do things.

We know how sensible this matter is, this is why we are obligated to ensure the highest
level of compatibility and a fast support in case anything happen. We are probably going to be
less forgiven in case of bugs than the original urllib3. For good~ish reasons, we know.

The matter is taken with utmost seriousness and everyone can inspect this package at will.

We regularly test this fork against the most used packages (that depend on urllib3, especially those who plunged deep into urllib3 internals).

Finally, rare is someone "fully aware" of their transitive dependencies. And "urllib3" is forced
into your environments regardless of your preferences.

- **Wasn't there any other solution than having an in-place fork?**

We assessed many solutions but none were completely satisfying.
We agree that this solution isn't perfect and actually put a lot of pressure on us (urllib3-future).

Here are some of the reasons (not exhaustive) we choose to work this way:

> A) Some major companies may not be able to touch the production code but can "change/swap" dependencies.

> B) urllib3-future main purpose is to fuel Niquests, which is itself a drop-in replacement of Requests.
  And there's more than 100 packages commonly used that plug into Requests, but the code (of the packages) invoke urllib3
  So... We cannot fork those 100+ projects to patch urllib3 usage, it is impossible at the moment, given our means.
  Requests trapped us, and there should be a way to escape the nonsense "migrate" to another http client that reinvent
  basic things and interactions.

> C) We don't have to reinvent the wheel.

> D) Some of our partners started noticing that HTTP/1 started to be disabled by some webservices in favor of HTTP/2+
  So, this fork can unblock them at (almost) zero cost.

- **OK... then what do I gain from this?**

1. It is faster than its counterpart, we measured gain up to 2X faster in a multithreaded environment using a http2 endpoint.
2. It works well with gevent / does not conflict. We do not use the standard queue class from stdlib as it does not fit http2+ constraints.
3. Leveraging recent protocols like http2 and http3 transparently. Code and behaviors does not change one bit.
4. You do not depend on the standard library to emit http/1 requests, and that is actually a good news. http.client 
  has numerous known flaws but cannot be fixed as we speak. (e.g. urllib3 is based on http.client)
5. There a ton of other improvement you may leverage, but for that you will need to migrate to Niquests or update your code
  to enable specific capabilities, like but not limited to: "DNS over QUIC, HTTP" / "Happy Eyeballs" / "Native Asyncio" / "Advanced Multiplexing".
6. Non-blocking IO with concurrent streams/requests. And yes, transparently.
7. It relaxes some constraints established by upstream in their version 2, thus making it easier to upgrade from version 1.

- **Is this funded?**

Yes! We have some funds coming in regularly to ensure its sustainability.

- **How can I restore urllib3 to the "legacy" version?**

You can easily do so:

```
# remove both
python -m pip uninstall -y urllib3 urllib3-future
# reinstate legacy urllib3
python -m pip install urllib3
```

OK! How to let them both?

```
# remove both
python -m pip uninstall -y urllib3 urllib3-future
# install urllib3-future
python -m pip install urllib3-future
# reinstate legacy urllib3
python -m pip install urllib3
```

The order is (actually) important.

Super! But how can I do that when installing something that requires somewhere urllib3-future?

Let's say you want to install Niquests and keep BOTH urllib3 and urllib3-future, do:

```
URLLIB3_NO_OVERRIDE=true pip install niquests --no-binary urllib3-future
```

This applies to every package you wish to install and brings indirectly urllib3-future.

- **Can you guarantee us that everything will go smooth?**

Guarantee is a strong word with a lot of (legal) implication. We cannot offer a "guarantee".
But, we answer and solve issues in a timely manner as you may have seen in our tracker.

We take a lot of precaution with this fork, and we welcome any contribution at the sole condition
that you don't break the compatibility between the projects. Namely, urllib3 and urllib3-future.

Every software is subject to bugs no matter what we do.

This being said, rest assured, we kept all the tests from urllib3 to ensure that what was
guaranteed by upstream is also carefully watched down there. See the CI/pipeline for yourself.

In addition to that, we enforced key integration tests to watch how urllib3-future act with some critical projects.

Top-priorities issues are those impacting users with the "shadowing" part. Meaning, if a user is suffering
an error or something that ends up causing an undesirable outcome from a third-party library that leverage urllib3.

- **OS Package Managers**

Fellow OS package maintainers, you cannot _just_ build and ship this package to your package registry.
As it override `urllib3` and due to its current criticality, you'll have to set:

`URLLIB3_NO_OVERRIDE=true python -m build`. Set `URLLIB3_NO_OVERRIDE` variable with "**true**" in it.

It will prevent the override.

## Compatibility with downstream

You should _always_ install the downstream project prior to this fork. It is compatible with any program that use urllib3 directly or indirectly.

e.g. I want `requests` to be use this package.

```
python -m pip install requests
python -m pip install urllib3.future
```

Nowadays, we suggest using the package [**Niquests**](https://github.com/jawah/niquests) as a drop-in replacement for **Requests**. 
It leverages urllib3.future capabilities appropriately.

## Testing

To ensure that we serve HTTP/1.1, HTTP/2 and HTTP/3 correctly we use containers
that simulate a real-world server that is not made with Python.

Although it is not made mandatory to run the test suite, it is strongly recommended.

You should have docker installed and the compose plugin available. The rest will be handled automatically.

```
python -m pip install nox
nox -s test-3.11
```

The nox script will attempt to start a Traefik server along with a httpbin instance.
Both Traefik and httpbin are written in golang.

You may prevent the containers from starting by passing the following environment variable:

```
TRAEFIK_HTTPBIN_ENABLE=false nox -s test-3.11
```

## Documentation

urllib3.future has usage and reference documentation at [urllib3future.readthedocs.io](https://urllib3future.readthedocs.io).

## Contributing

urllib3.future happily accepts contributions.

## Security Disclosures

To report a security vulnerability, please use the GitHub advisory disclosure form.

## Sponsorship

If your company benefits from this library, please consider sponsoring its
development.
