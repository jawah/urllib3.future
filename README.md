<h1 align="center">

![urllib3](https://github.com/jawah/urllib3.future/raw/main/docs/_static/logo.png)

</h1>

<p align="center">
  <a href="https://pypi.org/project/urllib3-future"><img alt="PyPI Version" src="https://img.shields.io/pypi/v/urllib3-future.svg?maxAge=86400" /></a>
  <a href="https://pypi.org/project/urllib3-future"><img alt="Python Versions" src="https://img.shields.io/pypi/pyversions/urllib3-future.svg?maxAge=86400" /></a>
  <br><small>urllib3.future is as BoringSSL is to OpenSSL but to urllib3 (except support is available!)</small>
</p>

urllib3 is a powerful, *user-friendly* HTTP client for Python. urllib3.future goes beyond supported features while remaining
mostly compatible.
urllib3.future brings many critical features that are missing from the Python
standard libraries:

- Thread safety.
- Connection pooling.
- Client-side SSL/TLS verification.
- File uploads with multipart encoding.
- Helpers for retrying requests and dealing with HTTP redirects.
- Support for gzip, deflate, brotli, and zstd encoding.
- HTTP/1.1, HTTP/2 and HTTP/3 support.
- Proxy support for HTTP and SOCKS.
- 100% test coverage.

urllib3 is powerful and easy to use:

```python
>>> import urllib3
>>> resp = urllib3.request("GET", "https://httpbin.org/robots.txt")
>>> resp.status
200
>>> resp.data
b"User-agent: *\nDisallow: /deny\n"
>>> resp.version
20
```

## Installing

urllib3.future can be installed with [pip](https://pip.pypa.io):

```bash
$ python -m pip install urllib3.future
```

⚠️ Installing urllib3.future shadows the actual urllib3 package (_depending on installation order_) and you should
carefully weigh the impacts. The semver will always be like _MAJOR.MINOR.9PP_ like 2.0.941, the patch node
is always greater or equal to 900.

Support for bugs or improvements is served in this repository. We regularly sync this fork
with the main branch of urllib3/urllib3.

## Compatibility with downstream

You should _always_ install the downstream project prior to this fork.

e.g. I want `requests` to be use this package.

```
python -m pip install requests
python -m pip install urllib3.future
```

| Package          | Is compatible? | Notes                                                                                                                                           |
|------------------|----------------|-------------------------------------------------------------------------------------------------------------------------------------------------|
| requests         | ✅              | Invalid chunked transmission may raises ConnectionError instead of ChunkedEncodingError. Use of Session() is required to enable HTTP/3 support. |
| HTTPie           | ✅              | Require plugin `httpie-next` to be installed or wont be able to upgrade to HTTP/3 (QUIC/Alt-Svc Cache Layer)                                    |
| pip              | 🛑             | Cannot use the fork because of vendored urllib3 v1.x                                                                                            |
| openapigenerator | ✅              | Simply patch generated `setup.py` requirement and replace urllib3 to urllib3.future                                                             |

Want to report an incompatibility? Open an issue in that repository.
All projects that depends on listed *compatible* package should work as-is.

## Documentation

urllib3.future has usage and reference documentation at [urllib3future.readthedocs.io](https://urllib3future.readthedocs.io).

## Contributing

urllib3.future happily accepts contributions.

## Security Disclosures

To report a security vulnerability, please use the GitHub advisory disclosure form.

## Sponsorship

If your company benefits from this library, please consider sponsoring its
development.
