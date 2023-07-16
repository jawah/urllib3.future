urllib3
=======

.. toctree::
   :hidden:
   :maxdepth: 3

   v2-migration-guide
   user-guide
   advanced-usage
   reference/index
   contributing
   changelog

urllib3 is a powerful, *user-friendly* HTTP client for Python.

urllib3 brings many critical features that are missing from the Python
standard libraries:

- Thread safety.
- Connection pooling.
- Client-side TLS/SSL verification.
- File uploads with multipart encoding.
- Helpers for retrying requests and dealing with HTTP redirects.
- Support for gzip, deflate, brotli, and zstd encoding.
- HTTP/1.1, HTTP/2 and HTTP/3 support.
- Proxy support for HTTP and SOCKS.
- 100% test coverage.

urllib3 is powerful and easy to use:

.. code-block:: pycon

   >>> import urllib3
   >>> resp = urllib3.request("GET", "https://httpbin.org/robots.txt")
   >>> resp.status
   200
   >>> resp.data
   b"User-agent: *\nDisallow: /deny\n"

Installing
----------

urllib3.future can be installed with `pip <https://pip.pypa.io>`_

.. code-block:: bash

  $ python -m pip install urllib3.future

Usage
-----

The :doc:`user-guide` is the place to go to learn how to use the library and
accomplish common tasks. The more in-depth :doc:`advanced-usage` guide is the place to go for lower-level tweaking.

The :doc:`reference/index` documentation provides API-level documentation.

License
-------

urllib3 is made available under the MIT License. For more details, see `LICENSE.txt <https://github.com/urllib3/urllib3/blob/master/LICENSE.txt>`_.

Contributing
------------

We happily welcome contributions, please see :doc:`contributing` for details.
