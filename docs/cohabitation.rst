Cohabitation with upstream urllib3
==================================

**You can use urllib3.future while keeping upstream urllib3 in place.**
Cohabitation gives each implementation its own namespace, so your imports
choose which one you use:

.. code-block:: python

   import urllib3         # upstream urllib3
   import urllib3_future  # urllib3.future

Niquests uses ``urllib3_future`` internally. Requests and other packages that
import ``urllib3`` continue using upstream urllib3. The fork's HTTP/2, HTTP/3,
async, and transport features remain available through ``urllib3_future``.

This guide explains the choice and shows two supported installation methods:
building from the PyPI source distribution, or installing a prebuilt
``+isolation`` wheel from the project's alternative index.

.. contents:: On this page
   :local:
   :depth: 2

Why there are two installation modes
------------------------------------

urllib3.future is an independently maintained fork of urllib3. It preserves
the synchronous API while adding the protocol and transport capabilities
required by Niquests. The work began with an experimental upstream proposal;
the `Niquests FAQ <https://niquests.readthedocs.io/en/latest/community/faq.html#we-tried-upstream-first>`_
describes that history and the decision to continue independently.

In-place replacement is the default because we want existing integrations to
work out of the box. Niquests extends Requests, whose ecosystem includes
plugins that import urllib3 directly, exchange its response and exception
objects, or depend on its types being identical. The default wheel provides
the ``urllib3`` namespace so these integrations share one implementation,
preserving that compatibility without requiring users to adapt each one.

This default is an **environment-wide choice**: every package importing
``urllib3`` receives urllib3.future. An inspectable ``.pth`` startup file
selects the implementation before application imports begin. Python packaging
has no standard replacement declaration that makes these two distributions
interchangeable. An extra could also be enabled by a transitive dependency,
while runtime injection would make behavior depend on when imports occur.
The `namespace rationale in the Niquests FAQ
<https://niquests.readthedocs.io/en/latest/community/faq.html#why-urllib3-future-provides-the-urllib3-namespace>`_
discusses these alternatives in more detail.

**Cohabitation is the choice when you want separate namespaces.** For example,
your application may depend on Niquests indirectly while expecting existing
Requests integrations to continue using upstream urllib3. Both installation
methods below support that choice: they install only ``urllib3_future`` for
the fork, omit its ``.pth`` startup file, and leave upstream urllib3's package
separate. This changes the packaging; it does not remove any urllib3.future
features.

What separation means for your application
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Requests and Niquests can make requests independently in the same process.
Their transports have separate Python classes, however: an upstream
``urllib3`` exception or response is not the corresponding ``urllib3_future``
type. Matching names and APIs do not make these class objects identical,
which matters for ``isinstance`` checks and exception handlers. Niquests
translates supported upstream ``Retry`` and ``Timeout`` objects. Requests
extensions that exchange other transport objects with Niquests may need
adaptation.

If you maintain a Niquests extension, import its transport through Niquests:

.. code-block:: python

   from niquests.packages import urllib3

This selects the same implementation as Niquests in either installation mode.
For application code that specifically needs the fork, use
``import urllib3_future``.

Compatibility is a release requirement. The project runs the inherited test
suite, downstream suites including Requests and Niquests, and HTTP/1.1,
HTTP/2, and HTTP/3 integration tests. Applicable upstream bug fixes and
security patches are reviewed and incorporated. You can inspect the
`downstream checks <https://github.com/jawah/urllib3.future/actions/workflows/integration.yml>`_
and :doc:`changelog`. These checks provide concrete compatibility evidence;
an integration relying on private internals still needs its own validation.

Choose an installation method
-----------------------------

.. list-table:: Both methods provide the same separate namespace
   :header-rows: 1
   :widths: 25 35 40

   * - Method
     - Where it comes from
     - How your project records the choice
   * - :ref:`Source distribution <cohabitation-source>`
     - The release sdist on PyPI, built locally
     - A source-only setting and ``URLLIB3_NO_OVERRIDE=1`` during the build
   * - :ref:`Prebuilt wheel <cohabitation-wheel>`
     - The project's GitHub Pages isolation index
     - A complete ``+isolation`` version pin or a package-specific index binding

Choose one method for your environment. The source route works with releases
already on PyPI and is also suitable for distribution packagers. The wheel
route avoids building urllib3.future locally and provides an attestation you
can verify before installation.

Start with a fresh environment
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If urllib3.future is already installed in drop-in mode, recreate the virtual
environment with your chosen recipe. A build setting cannot change an
already-installed wheel. A fresh environment gives each distribution clear
ownership of its files.

With pip, create an environment in an unused directory. On Linux or macOS:

.. code-block:: bash

   python -m venv .venv
   source .venv/bin/activate

On Windows, in PowerShell:

.. code-block:: powershell

   py -m venv .venv
   .\.venv\Scripts\Activate.ps1

uv, Poetry, and PDM can manage the project environment for you. Configure the
chosen method before the first install. Creating a virtual environment over
an existing directory does not clear its installed packages.

.. _cohabitation-source:

Option 1: build from the PyPI source distribution
-------------------------------------------------

Select the source distribution for ``urllib3-future`` and set
``URLLIB3_NO_OVERRIDE=1`` for its build. Other dependencies can still use
wheels. The flag is a **build-time setting**; your application does not need
it at runtime.

The examples explicitly install upstream ``urllib3`` alongside the fork.
Add Niquests, Requests, or other application dependencies as needed.

pip
~~~

Save the following in ``requirements-cohabitation.txt``:

.. code-block:: text

   --no-binary=urllib3-future
   urllib3-future
   urllib3

On Linux or macOS, install with:

.. code-block:: bash

   URLLIB3_NO_OVERRIDE=1 \
   python -m pip install --no-cache-dir -r requirements-cohabitation.txt

On Windows, in PowerShell:

.. code-block:: powershell

   $env:URLLIB3_NO_OVERRIDE = "1"
   python -m pip install --no-cache-dir -r requirements-cohabitation.txt
   Remove-Item Env:URLLIB3_NO_OVERRIDE

``--no-cache-dir`` ensures the installation builds afresh. Keep the source-only
setting and supply the build flag in subsequent installs and upgrades,
including CI and deployment builds. Pin dependency versions according to your
project's usual policy; a version pin alone does not record the build flag.

uv
~~

Store both choices in your project's ``pyproject.toml``:

.. code-block:: toml

   [tool.uv]
   no-binary-package = ["urllib3-future"]
   extra-build-variables = { "urllib3-future" = { URLLIB3_NO_OVERRIDE = "1" } }

Then install:

.. code-block:: bash

   uv add urllib3-future urllib3

Commit ``pyproject.toml`` and ``uv.lock``. uv selects the source distribution
and supplies the build flag automatically, including when urllib3.future is
a dependency of Niquests. The same configuration works on Linux, macOS, and
Windows, with no shell environment variable needed. Use ``uv sync --locked``
to reproduce the environment.

Poetry
~~~~~~

Configure source builds for urllib3.future, then install with the build flag:

.. code-block:: bash

   poetry config --local installer.no-binary urllib3-future
   URLLIB3_NO_OVERRIDE=1 poetry add urllib3-future urllib3

Keep ``poetry.toml``, ``pyproject.toml``, and ``poetry.lock`` with the project.
Supply the environment variable whenever installing, syncing, or upgrading.
In PowerShell, set ``$env:URLLIB3_NO_OVERRIDE = "1"`` before running the Poetry
command instead of using the shell prefix above.

PDM
~~~

Add the source-build setting to ``pyproject.toml``:

.. code-block:: toml

   [tool.pdm.resolution]
   no-binary = "urllib3-future"

Then install with the build flag:

.. code-block:: bash

   URLLIB3_NO_OVERRIDE=1 pdm add urllib3-future urllib3

Keep ``pyproject.toml`` and ``pdm.lock`` with the project and supply the build
flag on subsequent installs, syncs, and upgrades. In PowerShell, set
``$env:URLLIB3_NO_OVERRIDE = "1"`` before running the PDM command.

.. _cohabitation-wheel:

Option 2: install a prebuilt alternative wheel
----------------------------------------------

The `isolation index <https://jawah.github.io/urllib3.future/isolation/>`_ lists the
available cohabitation wheels, hashes, and attestations. Each wheel is built
from the corresponding release sdist. Its ``+isolation`` version suffix records
the namespace isolation choice in installed-package listings and lockfiles; its
dependency requirements stay the same.

Choose a version listed in the index. The examples use ``2.25.900+isolation``;
use the source method if your desired release is not yet listed. This route
needs no build flag or source-only setting.

pip
~~~

Save this in ``requirements-isolation.txt``, retaining the **complete local version**:

.. code-block:: text

   --extra-index-url https://jawah.github.io/urllib3.future/isolation/simple/
   urllib3-future==2.25.900+isolation
   urllib3

Install with the same command on Linux, macOS, or Windows:

.. code-block:: bash

   python -m pip install -r requirements-isolation.txt

The exact ``+isolation`` pin preserves your choice. If that wheel is unavailable,
pip fails to resolve it. An extra index on its own, or a public-version pin
such as ``==2.25.900``, can also select the drop-in PyPI build. Retain the full
pin when adding dependencies or upgrading.

Alternatively, copy a direct wheel URL, including its ``#sha256=...`` fragment,
from the index into your requirements file. This selects the exact artifact
without configuring an extra index. Keep its hash in deployment lockfiles too.

uv
~~

Bind only urllib3.future to the isolation index in ``pyproject.toml``:

.. code-block:: toml

   [tool.uv.sources]
   urllib3-future = { index = "urllib3-future-isolation" }

   [[tool.uv.index]]
   name = "urllib3-future-isolation"
   url = "https://jawah.github.io/urllib3.future/isolation/simple/"
   explicit = true

Then install:

.. code-block:: bash

   uv add urllib3-future urllib3

Commit ``pyproject.toml`` and ``uv.lock``. The binding also covers Niquests'
dependency on urllib3.future. ``explicit = true`` keeps unrelated packages on
their usual indexes. If no compatible isolation wheel is available, resolution
fails. Reproduce the locked environment with ``uv sync --locked``.

Poetry
~~~~~~

Add an explicit source and bind urllib3.future to it:

.. code-block:: bash

   poetry source add --priority=explicit urllib3-future-isolation https://jawah.github.io/urllib3.future/isolation/simple/
   poetry add --source urllib3-future-isolation urllib3-future
   poetry add urllib3

Keep ``pyproject.toml`` and ``poetry.lock`` with the project. The explicit
dependency binds the fork to the isolation index even when Niquests also requires
it. Upstream urllib3 and other dependencies use their usual sources.

PDM
~~~

Bind the package to the isolation index in ``pyproject.toml``:

.. code-block:: toml

   [[tool.pdm.source]]
   name = "urllib3-future-isolation"
   url = "https://jawah.github.io/urllib3.future/isolation/simple/"
   include_packages = ["urllib3-future"]

Then install:

.. code-block:: bash

   pdm add urllib3-future urllib3

Keep ``pyproject.toml`` and ``pdm.lock`` with the project. The package binding
restricts urllib3.future to the isolation index, including when Niquests depends
on it.

These package-source settings belong to your application. They do not
propagate through a published library's dependency metadata.

Verify your installation
------------------------

After either method, save this as ``check_cohabitation.py``:

.. code-block:: python

   import urllib3
   import urllib3_future

   print("Upstream urllib3:", urllib3.__version__, urllib3.__file__)
   print("urllib3.future:", urllib3_future.__version__, urllib3_future.__file__)

   assert not hasattr(urllib3, "AsyncPoolManager")
   assert hasattr(urllib3_future, "AsyncPoolManager")
   print("Both implementations are available in their own namespaces.")

Run it in your environment:

.. code-block:: bash

   python check_cohabitation.py
   python -m pip check

With uv, use ``uv run --no-sync python check_cohabitation.py`` and
``uv pip check``. With Poetry or PDM, run the script through ``poetry run`` or
``pdm run``. The upstream line should identify upstream urllib3; the fork
line should identify urllib3.future. Both installation methods preserve the
library's usual ``__version__`` value, such as ``2.25.900``, for compatibility
with downstream version checks. The alternative wheel's ``+isolation`` label
appears in its distribution metadata, which you can inspect with:

.. code-block:: bash

   python -m pip show urllib3-future

If you have installed both Niquests and Requests, you can also check which
transport each selected:

.. code-block:: python

   import niquests
   import requests
   import urllib3
   import urllib3_future

   assert niquests.packages.urllib3 is urllib3_future
   assert requests.packages.urllib3 is urllib3

The project's packaging checks exercise requests from both clients in one
process and verify that uninstalling either urllib3 distribution leaves the
other's package intact. When removing a distribution, remember its consumers:
Niquests needs urllib3.future, while Requests needs upstream urllib3 in this mode.

Keep your choice when upgrading
-------------------------------

For source builds with pip, Poetry, or PDM, retain the source-only setting and
supply ``URLLIB3_NO_OVERRIDE=1`` during installs and upgrades. With pip, add
``--upgrade`` to the demonstrated install command when updating unpinned
dependencies.

For prebuilt wheels with pip, choose a newly listed release, update the full
``==VERSION+isolation`` pin, and install from the same requirements file. With
Poetry or PDM, retain the package-source binding when updating your lockfile.

With uv, either method uses:

.. code-block:: bash

   uv lock --upgrade-package urllib3-future
   uv sync --locked

The configured build settings or index binding stay in place. Keep the same
configuration in local development, CI, and deployment. If an isolation release is
temporarily unavailable, retain your current lock or choose a source build
in a fresh environment.

Verify a wheel's provenance
---------------------------

Each published isolation wheel is covered by the same ``multiple.intoto.jsonl``
SLSA provenance file as the regular wheel and source distribution. The isolation
index links to this file on the matching GitHub release.

To verify before installing, download the wheel from the index and the linked
``multiple.intoto.jsonl`` file. With the `GitHub CLI <https://cli.github.com/>`_,
use the matching release tag (without ``+isolation``):

.. code-block:: bash

   gh attestation verify urllib3_future-2.25.900+isolation-py3-none-any.whl \
     --bundle multiple.intoto.jsonl \
     --repo jawah/urllib3.future \
     --source-ref refs/tags/2.25.900 \
     --signer-workflow slsa-framework/slsa-github-generator/.github/workflows/generator_generic_slsa3.yml \
     --predicate-type https://slsa.dev/provenance/v0.2

On PowerShell, enter the command on one line or use backticks for line
continuation. To require a specific source commit, add
``--source-digest <expected-commit-sha>``. After successful verification,
install the downloaded wheel:

.. code-block:: bash

   python -m pip install ./urllib3_future-2.25.900+isolation-py3-none-any.whl urllib3

Retain the same ``+isolation`` pin in project requirements for subsequent installs.
pip and uv do not verify attestations automatically. Index hashes check the
downloaded bytes; verifying the attestation also checks their provenance
against the repository and release you selected, and the trusted SLSA generator.
