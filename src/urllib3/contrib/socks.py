"""
This module contains provisional support for SOCKS proxies from within
urllib3. This module supports SOCKS4, SOCKS4A (an extension of SOCKS4), and
SOCKS5. To enable its functionality, either install PySocks or install this
module with the ``socks`` extra.

The SOCKS implementation supports the full range of urllib3 features. It also
supports the following SOCKS features:

- SOCKS4A (``proxy_url='socks4a://...``)
- SOCKS4 (``proxy_url='socks4://...``)
- SOCKS5 with remote DNS (``proxy_url='socks5h://...``)
- SOCKS5 with local DNS (``proxy_url='socks5://...``)
- Usernames and passwords for the SOCKS proxy

.. note::
   It is recommended to use ``socks5h://`` or ``socks4a://`` schemes in
   your ``proxy_url`` to ensure that DNS resolution is done from the remote
   server instead of client-side when connecting to a domain name.

SOCKS4 supports IPv4 and domain names with the SOCKS4A extension. SOCKS5
supports IPv4, IPv6, and domain names.

When connecting to a SOCKS4 proxy the ``username`` portion of the ``proxy_url``
will be sent as the ``userid`` section of the SOCKS request:

.. code-block:: python

    proxy_url="socks4a://<userid>@proxy-host"

When connecting to a SOCKS5 proxy the ``username`` and ``password`` portion
of the ``proxy_url`` will be sent as the username/password to authenticate
with the proxy:

.. code-block:: python

    proxy_url="socks5h://<username>:<password>@proxy-host"

"""

from __future__ import annotations

from python_socks import (  # type: ignore[import-untyped]
    ProxyConnectionError,
    ProxyError,
    ProxyTimeoutError,
    ProxyType,
)

try:
    from python_socks.sync import Proxy  # type: ignore[import-untyped]

    from ._socks_override import AsyncioProxy
except ImportError:
    import warnings

    from ..exceptions import DependencyWarning

    warnings.warn(
        (
            "SOCKS support in urllib3.future requires the installation of an optional "
            "dependency: python-socks. For more information, see "
            "https://urllib3future.readthedocs.io/en/latest/contrib.html#socks-proxies"
        ),
        DependencyWarning,
    )
    raise

import typing
from socket import socket
from socket import timeout as SocketTimeout

# asynchronous part
from .._async.connection import AsyncHTTPConnection, AsyncHTTPSConnection
from .._async.connectionpool import AsyncHTTPConnectionPool, AsyncHTTPSConnectionPool
from .._async.poolmanager import AsyncPoolManager
from .._typing import _TYPE_SOCKS_OPTIONS

# synchronous part
from ..connection import HTTPConnection, HTTPSConnection
from ..connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from ..contrib.ssa import AsyncSocket
from ..exceptions import ConnectTimeoutError, NewConnectionError
from ..poolmanager import PoolManager
from ..util.url import parse_url

try:
    import ssl
except ImportError:
    ssl = None  # type: ignore[assignment]


class SOCKSConnection(HTTPConnection):
    """
    A plain-text HTTP connection that connects via a SOCKS proxy.
    """

    def __init__(
        self,
        _socks_options: _TYPE_SOCKS_OPTIONS,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> None:
        self._socks_options = _socks_options
        super().__init__(*args, **kwargs)

    def _new_conn(self) -> socket:
        """
        Establish a new connection via the SOCKS proxy.
        """
        extra_kw: dict[str, typing.Any] = {}
        if self.source_address:
            extra_kw["source_address"] = self.source_address

        if self.socket_options:
            only_tcp_options = []

            for opt in self.socket_options:
                if len(opt) == 3:
                    only_tcp_options.append(opt)
                elif len(opt) == 4:
                    protocol: str = opt[3].lower()
                    if protocol == "udp":
                        continue
                    only_tcp_options.append(opt[:3])

            extra_kw["socket_options"] = only_tcp_options

        try:
            assert self._socks_options["proxy_host"] is not None
            assert self._socks_options["proxy_port"] is not None

            p = Proxy(
                proxy_type=self._socks_options["socks_version"],
                host=self._socks_options["proxy_host"],
                port=int(self._socks_options["proxy_port"]),
                username=self._socks_options["username"],
                password=self._socks_options["password"],
                rdns=self._socks_options["rdns"],
            )

            _socket = self._resolver.create_connection(
                (
                    self._socks_options["proxy_host"],
                    int(self._socks_options["proxy_port"]),
                ),
                timeout=self.timeout,
                source_address=self.source_address,
                socket_options=extra_kw["socket_options"],
                quic_upgrade_via_dns_rr=False,
                timing_hook=lambda _: setattr(self, "_connect_timings", _),
            )

            return p.connect(  # type: ignore[no-any-return]
                self.host,
                self.port,
                self.timeout,
                _socket,
            )
        except (SocketTimeout, ProxyTimeoutError) as e:
            raise ConnectTimeoutError(
                self,
                f"Connection to {self.host} timed out. (connect timeout={self.timeout})",
            ) from e

        except (ProxyConnectionError, ProxyError) as e:
            raise NewConnectionError(
                self, f"Failed to establish a new connection: {e}"
            ) from e

        except OSError as e:  # Defensive: PySocks should catch all these.
            raise NewConnectionError(
                self, f"Failed to establish a new connection: {e}"
            ) from e


# We don't need to duplicate the Verified/Unverified distinction from
# urllib3/connection.py here because the HTTPSConnection will already have been
# correctly set to either the Verified or Unverified form by that module. This
# means the SOCKSHTTPSConnection will automatically be the correct type.
class SOCKSHTTPSConnection(SOCKSConnection, HTTPSConnection):
    pass


class SOCKSHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = SOCKSConnection


class SOCKSHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = SOCKSHTTPSConnection


class SOCKSProxyManager(PoolManager):
    """
    A version of the urllib3 ProxyManager that routes connections via the
    defined SOCKS proxy.
    """

    pool_classes_by_scheme = {
        "http": SOCKSHTTPConnectionPool,
        "https": SOCKSHTTPSConnectionPool,
    }

    def __init__(
        self,
        proxy_url: str,
        username: str | None = None,
        password: str | None = None,
        num_pools: int = 10,
        headers: typing.Mapping[str, str] | None = None,
        **connection_pool_kw: typing.Any,
    ):
        parsed = parse_url(proxy_url)

        if username is None and password is None and parsed.auth is not None:
            split = parsed.auth.split(":")
            if len(split) == 2:
                username, password = split
        if parsed.scheme == "socks5":
            socks_version = ProxyType.SOCKS5
            rdns = False
        elif parsed.scheme == "socks5h":
            socks_version = ProxyType.SOCKS5
            rdns = True
        elif parsed.scheme == "socks4":
            socks_version = ProxyType.SOCKS4
            rdns = False
        elif parsed.scheme == "socks4a":
            socks_version = ProxyType.SOCKS4
            rdns = True
        else:
            raise ValueError(f"Unable to determine SOCKS version from {proxy_url}")

        self.proxy_url = proxy_url

        socks_options = {
            "socks_version": socks_version,
            "proxy_host": parsed.host,
            "proxy_port": parsed.port,
            "username": username,
            "password": password,
            "rdns": rdns,
        }
        connection_pool_kw["_socks_options"] = socks_options

        super().__init__(num_pools, headers, **connection_pool_kw)

        self.pool_classes_by_scheme = SOCKSProxyManager.pool_classes_by_scheme


class AsyncSOCKSConnection(AsyncHTTPConnection):
    """
    A plain-text HTTP connection that connects via a SOCKS proxy.
    """

    def __init__(
        self,
        _socks_options: _TYPE_SOCKS_OPTIONS,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> None:
        self._socks_options = _socks_options
        super().__init__(*args, **kwargs)

    async def _new_conn(self) -> AsyncSocket:  # type: ignore[override]
        """
        Establish a new connection via the SOCKS proxy.
        """
        extra_kw: dict[str, typing.Any] = {}
        if self.source_address:
            extra_kw["source_address"] = self.source_address

        if self.socket_options:
            only_tcp_options = []

            for opt in self.socket_options:
                if len(opt) == 3:
                    only_tcp_options.append(opt)
                elif len(opt) == 4:
                    protocol: str = opt[3].lower()
                    if protocol == "udp":
                        continue
                    only_tcp_options.append(opt[:3])

            extra_kw["socket_options"] = only_tcp_options

        try:
            assert self._socks_options["proxy_host"] is not None
            assert self._socks_options["proxy_port"] is not None

            p = AsyncioProxy(
                proxy_type=self._socks_options["socks_version"],
                host=self._socks_options["proxy_host"],
                port=int(self._socks_options["proxy_port"]),
                username=self._socks_options["username"],
                password=self._socks_options["password"],
                rdns=self._socks_options["rdns"],
            )

            _socket = await self._resolver.create_connection(
                (
                    self._socks_options["proxy_host"],
                    int(self._socks_options["proxy_port"]),
                ),
                timeout=self.timeout,
                source_address=self.source_address,
                socket_options=extra_kw["socket_options"],
                quic_upgrade_via_dns_rr=False,
                timing_hook=lambda _: setattr(self, "_connect_timings", _),
            )

            return await p.connect(
                self.host,
                self.port,
                self.timeout,
                _socket,
            )
        except (SocketTimeout, ProxyTimeoutError) as e:
            raise ConnectTimeoutError(
                self,
                f"Connection to {self.host} timed out. (connect timeout={self.timeout})",
            ) from e

        except (ProxyConnectionError, ProxyError) as e:
            raise NewConnectionError(
                self, f"Failed to establish a new connection: {e}"
            ) from e

        except OSError as e:  # Defensive: PySocks should catch all these.
            raise NewConnectionError(
                self, f"Failed to establish a new connection: {e}"
            ) from e


# We don't need to duplicate the Verified/Unverified distinction from
# urllib3/connection.py here because the HTTPSConnection will already have been
# correctly set to either the Verified or Unverified form by that module. This
# means the SOCKSHTTPSConnection will automatically be the correct type.
class AsyncSOCKSHTTPSConnection(AsyncSOCKSConnection, AsyncHTTPSConnection):
    pass


class AsyncSOCKSHTTPConnectionPool(AsyncHTTPConnectionPool):
    ConnectionCls = AsyncSOCKSConnection


class AsyncSOCKSHTTPSConnectionPool(AsyncHTTPSConnectionPool):
    ConnectionCls = AsyncSOCKSHTTPSConnection


class AsyncSOCKSProxyManager(AsyncPoolManager):
    """
    A version of the urllib3 ProxyManager that routes connections via the
    defined SOCKS proxy.
    """

    pool_classes_by_scheme = {
        "http": AsyncSOCKSHTTPConnectionPool,
        "https": AsyncSOCKSHTTPSConnectionPool,
    }

    def __init__(
        self,
        proxy_url: str,
        username: str | None = None,
        password: str | None = None,
        num_pools: int = 10,
        headers: typing.Mapping[str, str] | None = None,
        **connection_pool_kw: typing.Any,
    ):
        parsed = parse_url(proxy_url)

        if username is None and password is None and parsed.auth is not None:
            split = parsed.auth.split(":")
            if len(split) == 2:
                username, password = split
        if parsed.scheme == "socks5":
            socks_version = ProxyType.SOCKS5
            rdns = False
        elif parsed.scheme == "socks5h":
            socks_version = ProxyType.SOCKS5
            rdns = True
        elif parsed.scheme == "socks4":
            socks_version = ProxyType.SOCKS4
            rdns = False
        elif parsed.scheme == "socks4a":
            socks_version = ProxyType.SOCKS4
            rdns = True
        else:
            raise ValueError(f"Unable to determine SOCKS version from {proxy_url}")

        self.proxy_url = proxy_url

        socks_options = {
            "socks_version": socks_version,
            "proxy_host": parsed.host,
            "proxy_port": parsed.port,
            "username": username,
            "password": password,
            "rdns": rdns,
        }
        connection_pool_kw["_socks_options"] = socks_options

        super().__init__(num_pools, headers, **connection_pool_kw)

        self.pool_classes_by_scheme = AsyncSOCKSProxyManager.pool_classes_by_scheme
