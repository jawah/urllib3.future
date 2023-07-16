<h1 align="center">

![urllib3](https://github.com/jawah/urllib3.future/raw/main/docs/_static/logo.png)

</h1>

<p align="center">
  <a href="https://pypi.org/project/urllib3"><img alt="PyPI Version" src="https://img.shields.io/pypi/v/urllib3.svg?maxAge=86400" /></a>
  <a href="https://pypi.org/project/urllib3"><img alt="Python Versions" src="https://img.shields.io/pypi/pyversions/urllib3.svg?maxAge=86400" /></a>
  <a href="https://github.com/urllib3/urllib3/actions?query=workflow%3ACI"><img alt="Coverage Status" src="https://img.shields.io/badge/coverage-100%25-success" /></a>
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
carefully weight the impacts. The semver will always be like _MAJOR.MINOR.9PP_ like 2.0.941, the patch node
is always greater or equal to 900.

Support for bugs or improvements is served in this repository. We regularly sync this fork
with the main branch of urllib3/urllib3.

## Documentation

urllib3 has usage and reference documentation at [urllib3.readthedocs.io](https://urllib3.readthedocs.io).

## Contributing

urllib3.future happily accepts contributions.

## Security Disclosures

To report a security vulnerability, please use the GitHub advisory disclosure form.

## Sponsorship

If your company benefits from this library, please consider sponsoring its
development.
