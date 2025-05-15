from __future__ import annotations

import io
import typing
import warnings

if typing.TYPE_CHECKING:
    import ssl

from ...contrib.imcc import load_cert_chain as _ctx_load_cert_chain
from ...contrib.ssa import AsyncSocket, SSLAsyncSocket
from ...exceptions import SSLError
from ..ssl_ import (
    ALPN_PROTOCOLS,
    _CacheableSSLContext,
    _is_key_file_encrypted,
    create_urllib3_context,
)


class DummyLock:
    def __enter__(self) -> DummyLock:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore[no-untyped-def]
        pass


class _NoLock_CacheableSSLContext(_CacheableSSLContext):
    def __init__(self, maxsize: int | None = 32):
        super().__init__(maxsize=maxsize)
        self._lock = DummyLock()  # type: ignore[assignment]


_SSLContextCache = _NoLock_CacheableSSLContext()


async def ssl_wrap_socket(
    sock: AsyncSocket,
    keyfile: str | None = None,
    certfile: str | None = None,
    cert_reqs: int | None = None,
    ca_certs: str | None = None,
    server_hostname: str | None = None,
    ssl_version: int | None = None,
    ciphers: str | None = None,
    ssl_context: ssl.SSLContext | None = None,
    ca_cert_dir: str | None = None,
    key_password: str | None = None,
    ca_cert_data: None | str | bytes = None,
    tls_in_tls: bool = False,
    alpn_protocols: list[str] | None = None,
    certdata: str | bytes | None = None,
    keydata: str | bytes | None = None,
    sharable_ssl_context: dict[str, typing.Any] | None = None,
) -> SSLAsyncSocket:
    """
    All arguments except for server_hostname, ssl_context, and ca_cert_dir have
    the same meaning as they do when using :func:`ssl.wrap_socket`.

    :param server_hostname:
        When SNI is supported, the expected hostname of the certificate
    :param ssl_context:
        A pre-made :class:`SSLContext` object. If none is provided, one will
        be created using :func:`create_urllib3_context`.
    :param ciphers:
        A string of ciphers we wish the client to support.
    :param ca_cert_dir:
        A directory containing CA certificates in multiple separate files, as
        supported by OpenSSL's -CApath flag or the capath argument to
        SSLContext.load_verify_locations().
    :param key_password:
        Optional password if the keyfile is encrypted.
    :param ca_cert_data:
        Optional string containing CA certificates in PEM format suitable for
        passing as the cadata parameter to SSLContext.load_verify_locations()
    :param tls_in_tls:
        No-op in asynchronous mode. Call wrap_socket of the SSLAsyncSocket later.
    :param alpn_protocols:
        Manually specify other protocols to be announced during tls handshake.
    :param certdata:
        Specify an in-memory client intermediary certificate for mTLS.
    :param keydata:
        Specify an in-memory client intermediary key for mTLS.
    """
    context = ssl_context

    with _SSLContextCache.lock(
        keyfile,
        certfile,
        cert_reqs,
        ca_certs,
        ssl_version,
        ciphers,
        sharable_ssl_context,
        ca_cert_dir,
        alpn_protocols,
        certdata,
        keydata,
        key_password,
        ca_cert_data,
    ):
        cached_ctx = (
            _SSLContextCache.get() if sharable_ssl_context is not None else None
        )

        if cached_ctx is None:
            if context is None:
                # Note: This branch of code and all the variables in it are only used in tests.
                # We should consider deprecating and removing this code.
                context = create_urllib3_context(
                    ssl_version, cert_reqs, ciphers=ciphers
                )

            if ca_certs or ca_cert_dir or ca_cert_data:
                try:
                    context.load_verify_locations(ca_certs, ca_cert_dir, ca_cert_data)
                except OSError as e:
                    raise SSLError(e) from e

            elif ssl_context is None and hasattr(context, "load_default_certs"):
                # try to load OS default certs; works well on Windows.
                context.load_default_certs()

            # Attempt to detect if we get the goofy behavior of the
            # keyfile being encrypted and OpenSSL asking for the
            # passphrase via the terminal and instead error out.
            if keyfile and key_password is None and _is_key_file_encrypted(keyfile):
                raise SSLError("Client private key is encrypted, password is required")

            if certfile:
                if key_password is None:
                    context.load_cert_chain(certfile, keyfile)
                else:
                    context.load_cert_chain(certfile, keyfile, key_password)
            elif certdata and keydata:
                try:
                    _ctx_load_cert_chain(context, certdata, keydata, key_password)
                except io.UnsupportedOperation as e:
                    warnings.warn(
                        f"""Passing in-memory client/intermediary certificate for mTLS is unsupported on your platform.
                        Reason: {e}. It will be picked out if you upgrade to a QUIC connection.""",
                        UserWarning,
                    )

            try:
                context.set_alpn_protocols(alpn_protocols or ALPN_PROTOCOLS)
            except (
                NotImplementedError
            ):  # Defensive: in CI, we always have set_alpn_protocols
                pass

            if sharable_ssl_context is not None:
                _SSLContextCache.save(context)
        else:
            context = cached_ctx

    return await sock.wrap_socket(context, server_hostname=server_hostname)
