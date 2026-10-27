Contributing
============

Contributions to urllib3.future are welcome. Development takes place in the
`jawah/urllib3.future repository <https://github.com/jawah/urllib3.future>`_.
This guide describes the fork's tools and tests, including its synchronous
and asynchronous APIs and HTTP/1.1, HTTP/2, and HTTP/3 support.

For a bug, include a small reproducer, the urllib3.future and Python versions,
the operating system, and the traceback. For a substantial feature or a change
to connection ownership, pooling, or cancellation, start with an
`issue <https://github.com/jawah/urllib3.future/issues>`_ so we can discuss the
behavior and compatibility requirements before implementation.

Setting up your development environment
---------------------------------------

Fork the repository, clone your fork, and work from its root directory.
Use a virtual environment for development tools. For example, on POSIX systems:

.. code-block:: bash

   python3.12 -m venv .venv
   . .venv/bin/activate
   python -m pip install --upgrade nox
   nox --list-sessions

On Windows, create the environment with ``py -3.12 -m venv .venv`` and activate
it with ``.venv\Scripts\Activate.ps1`` in PowerShell.

Python 3.12 is an example here; choose an installed interpreter listed by
``nox --list-sessions``. The library still supports Python 3.7, while current
development tools may require a newer Python. Nox can run a test session with
a different interpreter from the one running Nox itself.

Nox installs the dependencies and the local package in a separate environment
for each session. This matters because the distribution is named
``urllib3-future`` but normally provides the ``urllib3`` import namespace.
Run tests through Nox to exercise the checkout with its intended dependencies.
Edit ``src/urllib3/``; the build hook generates the ``urllib3_future`` copy.
See :doc:`cohabitation` for the alternative installation mode.

Finding the relevant code
-------------------------

A request normally passes through ``PoolManager``, a connection pool, a
connection, and the protocol backend. The main locations are:

.. list-table::
   :header-rows: 1
   :widths: 45 55

   * - Location
     - Purpose
   * - ``src/urllib3/`` and ``src/urllib3/_async/``
     - Public managers, pools, connections, and responses.
   * - ``src/urllib3/backend/``
     - Connection I/O and the bridge to the protocol implementations.
   * - ``src/urllib3/contrib/hface/``
     - HTTP/1.1, HTTP/2, and HTTP/3 protocol adapters.
   * - ``src/urllib3/contrib/resolver/``
     - System, in-memory, UDP, TLS, HTTPS, and QUIC DNS resolvers.
   * - ``src/urllib3/contrib/webextensions/``
     - WebSocket, server-sent event, and raw stream extensions.
   * - ``src/urllib3/contrib/ssa/`` and ``src/urllib3/contrib/wasi/``
     - Async socket support and WASI socket adapters.
   * - ``src/urllib3/util/traffic_police.py`` and its ``_async/`` counterpart
     - Connection and pool ownership, selection, and concurrency.

Keep synchronous and asynchronous behavior aligned. Blocking operations need
an async counterpart; resolver, backend, and web extension subpackages also
have their own ``_async/`` implementations. Include both variants in a
regression when the behavior is shared.

Running the tests
-----------------

Start with a focused run using an installed interpreter:

.. code-block:: bash

   nox --reuse-existing-virtualenvs -s test-3.12 -- test/test_response.py
   nox --reuse-existing-virtualenvs -s test-3.12 -- test/with_dummyserver/test_https.py::TestHTTPS::test_simple
   nox --reuse-existing-virtualenvs -s test-3.12 -- test/with_dummyserver/asynchronous/test_https.py

Arguments after ``--`` are passed to pytest by the normal test sessions.
For example, select tests by name or disable worker processes for debugging:

.. code-block:: bash

   nox --reuse-existing-virtualenvs -s test-3.12 -- test/test_response.py -k decode
   nox --reuse-existing-virtualenvs -s test-3.12 -- test/with_dummyserver/test_socketlevel.py -n 0 -x

The normal sessions use pytest-xdist with ``worksteal`` scheduling: four workers
locally and two when ``CI`` is set. Override this with ``-n`` after ``--``.
``--reuse-existing-virtualenvs`` avoids recreating the environment but still
installs the current checkout. Do not add ``--no-install`` after changing
library code: these sessions test an installed build, so skipping installation
can leave you testing an older copy.

Run the full suite on one interpreter with:

.. code-block:: bash

   nox --reuse-existing-virtualenvs -s test-3.12

Use explicit sessions when testing several interpreters, such as
``-s test-3.12 test-3.14``. The ``test`` selection expands to all its configured
interpreter variants, including PyPy and free-threaded Python. You do not need
the entire CI matrix installed locally. See
`GitHub Actions <https://github.com/jawah/urllib3.future/actions>`_ for the
current OS, interpreter, and backend matrix.

Choosing a test suite
~~~~~~~~~~~~~~~~~~~~~

* ``test/test_*.py`` contains focused API and utility tests.
* ``test/contrib/`` includes resolver and transport tests. Some use local
  fixtures; others deliberately contact public DNS services, so this directory
  is not an offline-only suite.
* ``test/with_dummyserver/`` exercises clients against local servers. It includes
  Tornado handlers and controlled socket-level HTTP, TLS, WebSocket, and QUIC
  scenarios. Shared DNS fixtures also live in ``test/conftest.py``.
* ``test/with_traefik/`` tests negotiated protocols, multiplexing, proxies, and
  extensions against the Docker stack.
* Keep sync and async client cases in matching modules, with the async mirror
  under ``asynchronous/``. For example, ``test/test_response.py`` is mirrored by
  ``test/asynchronous/test_response.py``. The same layout applies inside
  ``contrib``, ``with_dummyserver``, and ``with_traefik``.
* ``test/within_wasi/`` runs inside WebAssembly components through the dedicated
  WASI runner, not normal pytest discovery.

Traefik and Docker
~~~~~~~~~~~~~~~~~~

For the full suite, or paths under ``test/with_traefik/``, Nox starts the
Compose stack, prepares test certificates, waits for readiness, and stops the
stack afterward. Install Docker with Compose and ensure its daemon is running:

.. code-block:: bash

   nox --reuse-existing-virtualenvs -s test-3.12 -- test/with_traefik/

The default stack in ``docker-compose.yaml`` contains Traefik, go-httpbin, and
HAProxy. Windows uses ``docker-compose.win.yaml`` with Windows containers and
has no HAProxy service; tests requiring that service are skipped there.
The port mappings are in those files, including TCP and UDP port 4443 for
HTTPS and HTTP/3. Keep the configured ports available.

The test cases provide their own ``httpbin.local`` resolver and CA settings;
no hosts-file edit is needed. If Docker publishes ports on a VM address instead
of loopback, set ``TRAEFIK_HTTPBIN_IPV4`` to that reachable address, as the macOS
CI jobs do.

Selecting a specific path outside ``test/with_traefik/`` normally avoids starting
Docker. To explicitly disable automatic startup, set
``TRAEFIK_HTTPBIN_ENABLE=false``. For example, in a POSIX shell:

.. code-block:: bash

   TRAEFIK_HTTPBIN_ENABLE=false nox --reuse-existing-virtualenvs -s test-3.12 -- test/with_dummyserver/

In PowerShell, set ``$env:TRAEFIK_HTTPBIN_ENABLE = "false"`` before the command.
This setting controls startup; it does not disable external DNS tests or stop
an already running stack. Check pytest's skip summary to see which protocol
and platform cases actually ran.

Specialized sessions
~~~~~~~~~~~~~~~~~~~~

Use the sessions relevant to your change; ``nox --list-sessions`` lists the
available interpreter variants.

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Example session
     - What it exercises
   * - ``test_udp_fallback-3.14``
     - Real HTTP/3 and DNS traffic with the Python UDP transport forced on.
   * - ``test_ws_fast-3.14``
     - The WebSocket suites with the fast backend instead of wsproto.
   * - ``test_rtls-3.12`` / ``test_utls-3.12``
     - Alternative TLS backends where their packages are supported.
   * - ``tracemalloc-3.12``
     - Allocation traces for resource warnings, without xdist workers.
   * - ``test_ssl_large_resources-3.12``
     - Dedicated sync and async large TLS response tests.
   * - ``test_pysocks-3.12`` / ``test_brotlipy-3``
     - Legacy optional-dependency compatibility.

The Linux Docker runners under ``ci/`` cover configurations that an ordinary
virtual environment cannot provide, including legacy OpenSSL, FIPS, static
OpenSSL, and native WASI. For example, ``./ci/run_wasi.sh`` starts the test stack,
builds the WASI image, and collects its coverage. See :doc:`wasi` for the
component runtime and capability model. That runner also needs Python 3.14
installed for its ``wasi_traefik`` Nox session.

For changes affecting downstream compatibility, use the existing sessions:

.. code-block:: bash

   nox --reuse-existing-virtualenvs -s downstream_requests
   nox --reuse-existing-virtualenvs -s downstream_niquests

There are also sessions for Botocore, Boto3, Sphinx, Docker SDK, and ClickHouse
Connect. They fetch those projects and run their tests against this checkout;
some require additional services. Their setup is recorded in ``noxfile.py``
and ``.github/workflows/integration.yml``.

Writing reliable regression tests
---------------------------------

Extend an existing module for the component being tested before adding a new
test file. Controlled HTTP/2 and HTTP/3 cases belong in the mirrored
``with_dummyserver/test_protolevel.py`` modules. Put shared fixtures in the
nearest ``conftest.py`` and reusable peer or synchronization helpers in helper
modules; do not import fixtures or helpers from another test module. The local
protocol peers are defined in ``test/with_dummyserver/conftest.py``.

Keep each test's client API explicit rather than parameterizing sync versus
async. A synchronous client test may still need an async test harness when its
peer runs on the test event loop; dispatch blocking client calls to an executor
in that case.

Prefer a public client operation against a local dummyserver or the Traefik
stack when testing network behavior. For malformed frames, incomplete messages,
or peer resets, use a controlled local protocol server. A narrowly injected
failure can exercise an otherwise hard-to-trigger OS error while keeping the
surrounding connection real. Avoid replacing the entire I/O and protocol path
just to reach a statement.

Use events or other explicit synchronization to establish ordering in concurrent
tests. Bound waits and worker joins so a failure produces a useful traceback.
Give connection setup enough time before testing a short read deadline, and use
the existing timing helpers and tolerances instead of exact elapsed-time
comparisons. Close responses, clients, sockets, and servers explicitly; do not
rely on CPython's immediate reference-count cleanup, which PyPy does not share.

When testing a protocol-specific behavior, assert the negotiated version so an
HTTP/2 or HTTP/3 regression cannot silently pass through HTTP/1.1 fallback.
Use the existing capability fixtures for genuinely unavailable protocols,
backends, and operating-system features.

Coverage
~~~~~~~~

The project measures statement coverage. Normal Nox test sessions collect
coverage and save ``.coverage.*`` files. A focused run or a single platform is
not expected to cover the whole project.

CI combines the OS, interpreter, TLS, UDP fallback, and WASI coverage before
applying its threshold. The current gate is **96%**, configured in
``.github/workflows/ci.yml``.

New behavior and bug fixes should have meaningful tests. Where a path exists
only for defensive compatibility and cannot reasonably be exercised in CI,
propose a narrow ``# Defensive: <reason>`` exclusion for review. Explain the
specific constraint; rarity alone is not a reason to exclude cleanup,
cancellation, framing, or retry behavior. Coverage exclusions are configured
in ``.coveragerc``.

Formatting and type checking
----------------------------

.. code-block:: bash

   nox --reuse-existing-virtualenvs -s lint

The lint session runs pre-commit and mypy. Ruff performs lint fixes and
formatting; the other hooks check YAML, debug statements, whitespace, spelling,
and GitHub Actions configuration. Hooks may edit files: review their changes and
rerun the session when needed. The ``format`` session currently calls the same
workflow.

For type checking alone:

.. code-block:: bash

   nox --reuse-existing-virtualenvs -s mypy

This checks ``dummyserver``, ``noxfile.py``, ``src/urllib3``, and ``test``. The
current pre-commit configuration excludes ``docs/`` and ``ci/``; build and
review documentation separately.

Running local proxies
---------------------

Proxy regressions should normally use the fixtures in ``dummyserver``. For
manual debugging, after running a test session, its environment also contains
the dependencies for the standalone proxy scripts. From the repository root
on POSIX systems:

.. code-block:: bash

   .nox/test-3-12/bin/python -m dummyserver.proxy 8080
   .nox/test-3-12/bin/python -m dummyserver.https_proxy 8443

Run these in separate terminals and stop them with Ctrl-C. The HTTPS proxy uses
the repository's test certificate. On Windows the interpreter is under
``.nox/test-3-12/Scripts/python.exe``. Adjust the environment path for the session
you ran.

Contributing to documentation
-----------------------------

Build the Sphinx documentation through Nox:

.. code-block:: bash

   nox --reuse-existing-virtualenvs -s docs

Open ``docs/_build/html/index.html`` to review the result. The build treats
Sphinx warnings as errors, but also inspect the rendered page: the Furo theme
already supplies page navigation, so do not add an inline ``contents`` block.

Write examples that can be copied and run. Include imports, show how to clean
up streaming responses, and use ``asyncio.run()`` for complete async scripts.
Use keyword arguments except for the request method and URL, and prefer HTTPS
unless the example needs HTTP. Use descriptive names such as ``pm``, ``pool``,
``conn``, and ``response`` consistently within an example.

Use ``example.com`` for illustrative URLs and a suitable public service only
when its behavior is needed. Do not promise a particular negotiated protocol,
response order, or timing unless the example actually enforces it. Test executable
examples against local fixtures where possible, and document any required
extras, certificates, or server configuration.

Changelog and pull requests
---------------------------

Keep each change focused, explain the user-visible behavior, and list the checks
you ran. Include a regression test for a bug fix and update the documentation
when the public behavior changes.

The changelog check uses Towncrier fragments under ``changelog/`` unless the PR
has the ``Skip Changelog`` label. For user-facing changes, follow the
`fragment instructions in this repository
<https://github.com/jawah/urllib3.future/blob/main/changelog/README.rst>`_.
Test-only and documentation-only changes normally need no release note; ask a
maintainer to apply ``Skip Changelog`` if you cannot label the PR yourself.
Maintainers also edit the assembled release notes in ``CHANGES.rst`` directly.
An edit there alone does not satisfy the fragment check without the label.

Releases
--------

Maintainers coordinate releases and the final release notes. Contributors do
not need to bump the version or publish artifacts as part of an ordinary PR.
The version is defined in ``src/urllib3/_version.py``, and ``towncrier.toml``
configures fragment assembly into ``CHANGES.rst``.

Creating a GitHub release triggers ``.github/workflows/publish.yml`` to build
and publish the distributions, with a separate cohabitation wheel and index.
The workflows and repository settings govern publication.
