from __future__ import annotations

import os
import sys
from datetime import date

# If extensions (or modules to document with autodoc) are in another directory,
# add these directories to sys.path here. If the directory is relative to the
# documentation root, use os.path.abspath to make it absolute, like shown here.

root_path = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, root_path)

# https://docs.readthedocs.io/en/stable/builds.html#build-environment
if "READTHEDOCS" in os.environ:
    import glob

    if glob.glob("../changelog/*.*.rst"):
        print("-- Found changes; running towncrier --", flush=True)
        import subprocess

        subprocess.run(
            ["towncrier", "--yes", "--date", "not released yet"], cwd="..", check=True
        )

import urllib3

# -- General configuration -----------------------------------------------------


# Add any Sphinx extension module names here, as strings. They can be extensions
# coming with Sphinx (named 'sphinx.ext.*') or your custom ones.
extensions = [
    "sphinx.ext.autodoc",
    "sphinx_copybutton",
    "sphinx.ext.doctest",
    "sphinx.ext.intersphinx",
    "sphinxext.opengraph",
]

# Open Graph metadata
ogp_title = "urllib3.future documentation"
ogp_type = "website"
ogp_social_cards = {"image": "images/logo.png", "line_color": "#F09837"}
ogp_description = "urllib3.future is as BoringSSL is to OpenSSL but to urllib3 (and support is actively provided!)"

# Test code blocks only when explicitly specified
doctest_test_doctest_blocks = ""

# Add any paths that contain templates here, relative to this directory.
templates_path = ["_templates"]

# The suffix of source filenames.
source_suffix = ".rst"

# The master toctree document.
master_doc = "index"

# General information about the project.
project = "urllib3.future"
copyright = f"{date.today().year}, Andrey Petrov"

# The short X.Y version.
version = urllib3.__version__
# The full version, including alpha/beta/rc tags.
release = version

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
exclude_patterns = ["_build"]

# The name of the Pygments (syntax highlighting) style to use.
pygments_style = "friendly"

# The base URL with a proper language and version.
html_baseurl = os.environ.get("READTHEDOCS_CANONICAL_URL", "/")

# The theme to use for HTML and HTML Help pages.  See the documentation for
# a list of builtin themes.
html_theme = "furo"
html_favicon = "images/favicon.png"

html_static_path = ["_static"]
html_theme_options = {
    "announcement": """
        <a style=\"text-decoration: none; color: white;\" 
           href=\"https://github.com/sponsors/Ousret\">
           <img src=\"/en/latest/_static/favicon.png\"/> Support urllib3.future on GitHub Sponsors
        </a>
    """,
    "sidebar_hide_name": True,
    "light_logo": "logo.png",
    "dark_logo": "logo.png",
}

intersphinx_mapping = {"python": ("https://docs.python.org/3", None)}

# Show typehints as content of the function or method
autodoc_typehints = "description"

# Warn about all references to unknown targets
nitpicky = True
# Except for these ones, which we expect to point to unknown targets:
nitpick_ignore = [
    ("py:class", "_TYPE_SOCKS_OPTIONS"),
    ("py:class", "_TYPE_SOCKET_OPTIONS"),
    ("py:class", "_TYPE_TIMEOUT"),
    ("py:class", "_TYPE_FIELD_VALUE"),
    ("py:class", "_TYPE_BODY"),
    ("py:class", "_TYPE_TIMEOUT_INTERNAL"),
    ("py:class", "_TYPE_FAILEDTELL"),
    ("py:class", "_TYPE_FIELD_VALUE_TUPLE"),
    ("py:class", "_TYPE_FIELDS"),
    ("py:class", "_TYPE_ENCODE_URL_FIELDS"),
    ("py:class", "_HttplibHTTPResponse"),
    ("py:class", "_HttplibHTTPMessage"),
    ("py:class", "Message"),
    ("py:class", "TracebackType"),
    ("py:class", "Literal"),
    ("py:class", "email.errors.MessageDefect"),
    ("py:class", "MessageDefect"),
    ("py:class", "http.client.HTTPMessage"),
    ("py:class", "RequestHistory"),
    ("py:class", "SSLTransportType"),
    ("py:class", "VerifyMode"),
    ("py:class", "_ssl._SSLContext"),
    ("py:func", "ssl.wrap_socket"),
    ("py:class", "urllib3._collections.HTTPHeaderDict"),
    ("py:class", "urllib3._collections.RecentlyUsedContainer"),
    ("py:class", "urllib3._request_methods.RequestMethods"),
    ("py:class", "urllib3.contrib.socks._TYPE_SOCKS_OPTIONS"),
    ("py:class", "urllib3.util.timeout._TYPE_DEFAULT"),
    ("py:class", "urllib3.util.request._TYPE_FAILEDTELL"),
    ("py:class", "BaseHTTPResponse"),
    ("py:class", "urllib3.response.BaseHTTPResponse"),
    ("py:class", "connection._TYPE_SOCKET_OPTIONS"),
    ("py:class", "urllib3.backend.httplib._PatchedHTTPConnection"),
    ("py:class", "urllib3.backend._base.LowLevelResponse"),
    ("py:class", "LowLevelResponse"),
    ("py:class", "QuicPreemptiveCacheType"),
    ("py:class", "socket.SocketKind"),
    ("py:class", "socket.AddressFamily"),
    ("py:class", "timedelta"),
    ("py:class", "TLSVersion"),
    ("py:class", "TrafficPolice"),
    ("py:class", "urllib3.util.traffic_police.TrafficPolice"),
    ("py:class", "urllib3._async.connection.AsyncHTTPConnection"),
    ("py:class", "urllib3._async.connection.AsyncHTTPSConnection"),
    ("py:class", "urllib3.backend._async._base.AsyncLowLevelResponse"),
    ("py:class", "urllib3._async.connectionpool.AsyncConnectionPool"),
    ("py:class", "urllib3._request_methods.AsyncRequestMethods"),
    ("py:class", "AsyncConnectionPool"),
    ("py:class", "AsyncHTTPConnection"),
    ("py:class", "AsyncLowLevelResponse"),
    ("py:class", "AsyncResolverDescription"),
    ("py:class", "AsyncBaseResolver"),
    ("py:class", "AsyncTrafficPolice"),
    ("py:attr", "HTTPResponse.data"),
    ("py:class", "_TYPE_PEER_CERT_RET_DICT"),
    ("py:class", "_TYPE_ASYNC_BODY"),
    ("py:class", "ExtensionFromHTTP"),
    ("py:class", "AsyncExtensionFromHTTP"),
]
