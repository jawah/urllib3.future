Migrating to urllib3.future
===========================

urllib3.future preserves the public synchronous urllib3 APIs used by many
applications while adding async APIs, HTTP/2 and HTTP/3, custom DNS resolvers,
and web extensions. This page describes migration to the fork. Upstream
urllib3's historical 2.0 release and deprecation schedule are separate from
this project's releases.

Choose the installation mode
----------------------------

The distribution name is ``urllib3-future``. Its default wheel provides the
``urllib3`` import namespace, so existing callers use the fork without changing
their imports:

.. code-block:: bash

    python -m pip install --upgrade urllib3-future

.. code-block:: python

    import urllib3

    print(urllib3.__version__)
    print(urllib3.__file__)

This is an environment-wide replacement. If upstream urllib3 must remain in
use by other dependencies, follow :doc:`cohabitation` to install an isolated
wheel or build an isolated source distribution. In that mode, application code
selects the fork with ``import urllib3_future``. A runtime environment variable
does not convert an already-installed default wheel into an isolated one.

The two distributions are not interchangeable dependency names to Python's
package resolver. Check the resolved environment and run your application's
tests, including integrations that exchange response, exception, retry, or
timeout objects between libraries.

Application changes to review
-----------------------------

* Reuse a manager for related requests. Pools are selected by origin and
  connection policy; see :ref:`pool-behavior` for the fork's defaults and limits.
* Finish reading or explicitly close streamed responses. ``release_conn()``
  alone does not discard an unread body. See :ref:`response-lifecycle` for
  HTTP/1.1 versus multiplexed-stream cleanup and the 2.25.903 shutdown behavior.
* HTTP/2 and HTTP/3 can be negotiated automatically. Inspect ``response.version``
  rather than assuming HTTP/1.1; use :ref:`protocol-selection` when restricting
  protocols is necessary.
* ``multiplexed=True`` can return a ``ResponsePromise`` or an immediate response
  when HTTP/1.1 is used. See :ref:`multiplexed-responses`.
* Synchronous ``read(-1)`` reads the remaining body. Async negative-size reads
  retain incremental behavior. See :ref:`response-read-semantics`.
* Async applications use ``AsyncPoolManager`` and await I/O methods, while
  ``stream()`` is consumed with ``async for``. See :doc:`async`.

Compatibility concerns from urllib3 1.x
---------------------------------------

The fork supports CPython and PyPy versions compatible with Python 3.7 or later,
subject to platform and optional-dependency support. Python 2 is not supported.
Review the :doc:`changelog` for changes between the versions you deploy.

The old ``urllib3.contrib.ntlmpool``, ``urllib3.contrib.pyopenssl``, and
``urllib3.contrib.securetransport`` integrations are not available. See
:ref:`tls-backends` for the supported TLS backends.

``HTTPResponse.getheaders()`` and ``getheader()`` remain as compatibility
methods in current releases. Prefer ``response.headers`` and
``response.headers.get(...)``. Older copies of this guide incorrectly promised
that all deprecated APIs would disappear in version 2.1.0. Historical version
numbers in inherited deprecation notices do not establish this fork's removal
schedule; consult its current release notes.

TLS verification is enabled by default, with TLS 1.2 as the default minimum.
Default hostname verification uses subject alternative names. Review custom
SSL contexts, legacy TLS endpoints, and the selected backend's capabilities
when migrating an application that overrides these defaults.

The fork does not inherit its protocol backend from ``http.client``. Public
compatibility does not guarantee that private response fields, socket objects,
or protocol internals have the same types or behavior as upstream urllib3.
Prefer public response methods and documented extension APIs.

Package maintainers
-------------------

If your package specifically requires the fork, declare that dependency under
its distribution name. For example, in ``pyproject.toml``:

.. code-block:: toml

    [project]
    name = "my-http-application"
    version = "0.1.0"
    dependencies = ["urllib3-future>=2.25.903,<3"]

This example chooses the default replacement installation. Projects that need
cohabitation should follow that guide's source-build or isolation-wheel recipes.
Test supported upstream and fork configurations in separate environments if your
package supports both implementations.

Enable deprecation warnings when testing:

.. code-block:: bash

    PYTHONWARNINGS=default python -m pytest tests/

Report incompatibilities with a minimal reproducer and the installed versions
of Python, urllib3-future, the calling library, and any optional TLS or proxy
backend. Issues can be opened on `GitHub
<https://github.com/jawah/urllib3.future/issues>`_.

Older dependency errors
-----------------------

* Docker SDK errors about an unexpected ``chunked`` argument were addressed in
  Docker SDK 6.1.0. Upgrade older SDK versions and check their current compatibility
  requirements.
* Requests Toolbelt 1.0.0 removed its obsolete Google App Engine integration.
  An import error involving ``gaecontrib`` can require updating the calling
  package as well as Requests Toolbelt.
* Replace imports of ``VerifiedHTTPSConnection`` with the public
  ``urllib3.connection.HTTPSConnection`` class.
* Remove the obsolete ``strict`` parameter and private ``HTTPResponse.strict``
  accesses from Python 3 integrations.

For the history and support policy of upstream urllib3 releases, consult the
`upstream migration guide
<https://urllib3.readthedocs.io/en/stable/v2-migration-guide.html>`_. This fork's
features and fixes are tracked in its own :doc:`changelog`.
