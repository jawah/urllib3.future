This directory contains changelog entries: short files that contain a small
**ReST**-formatted text that will be added to ``CHANGES.rst`` by `towncrier
<https://towncrier.readthedocs.io/en/latest/>`__.

The ``CHANGES.rst`` will be read by **users**, so this description should be aimed to
urllib3.future users instead of describing internal changes which are only relevant to the
developers.

Make sure to use full sentences in the **past tense** and use punctuation, examples::

    Added support for HTTPS proxies contacting HTTPS servers.

    Fixed incomplete response reads losing buffered decompressed data.

Each file should be named like ``<ISSUE>.<TYPE>.rst``, where ``<ISSUE>`` is an issue
number, and ``<TYPE>`` is one of the `five towncrier default types
<https://towncrier.readthedocs.io/en/latest/#news-fragments>`_

So for example: ``123.feature.rst``, ``456.bugfix.rst``.

If your pull request fixes an issue, use that number here. If there is no issue, then
after you submit the pull request and get the pull request number you can add a
changelog using that instead.

Test-only and documentation-only changes normally do not need a changelog entry.
Ask a maintainer to apply the ``Skip Changelog`` GitHub label if you cannot label
the pull request yourself. The CI check accepts a new fragment or this label;
editing ``CHANGES.rst`` alone does not satisfy it.

To preview assembled fragments without changing files, run ``towncrier build --draft``
in an environment containing Towncrier and the local package, such as an existing
Nox test environment. The configuration is in ``towncrier.toml``.

``nox -s docs`` renders the existing ``CHANGES.rst`` at
``docs/_build/html/changelog.html``; a local build does not assemble fragments.
Read the Docs builds assemble pending fragments in their build checkout before
rendering the changelog.
