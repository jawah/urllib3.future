from __future__ import annotations

import ssl
import datetime
import threading
import typing
from io import UnsupportedOperation
from unittest import mock

import pytest
import trustme

from urllib3._constant import MOZ_INTERMEDIATE_CIPHERS
from urllib3.contrib.anytls import ssl as active_ssl
from urllib3.exceptions import ProxySchemeUnsupported, SSLError
from urllib3.util import ssl_


def test_in_memory_client_certificate_without_available_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib3.contrib import imcc

    ca = trustme.CA()
    client = ca.issue_cert("client.example")
    monkeypatch.setattr(imcc, "SUPPORTED_METHODS", [])
    with pytest.raises(UnsupportedOperation, match="unable to initialize mTLS"):
        imcc.load_cert_chain(
            ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
            client.cert_chain_pems[0].bytes(),
            client.private_key_pem.bytes(),
        )


@pytest.fixture
def malformed_ca() -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtensionOID, NameOID

    ca = trustme.CA()
    key = serialization.load_pem_private_key(ca.private_key_pem.bytes(), None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Malformed CA")])
    # The outer certificate parses, but its Basic Constraints is not a SEQUENCE.
    # X509_check_ca() leaves INVALID_CERTIFICATE queued while returning false.
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime.datetime(2020, 1, 1))
        .not_valid_after(datetime.datetime(2035, 1, 1))
        .add_extension(
            x509.UnrecognizedExtension(ExtensionOID.BASIC_CONSTRAINTS, b"\x01\x01\xff"),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


@pytest.fixture
def tls_reader() -> ssl.SSLObject:
    ca = trustme.CA()
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ca.issue_cert("localhost").configure_cert(server_ctx)
    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ca.configure_trust(client_ctx)
    client_in, client_out, server_in, server_out = [ssl.MemoryBIO() for _ in range(4)]
    client = client_ctx.wrap_bio(client_in, client_out, server_hostname="localhost")
    server = server_ctx.wrap_bio(server_in, server_out, server_side=True)
    done = set()
    for _ in range(10):
        for obj in (client, server):
            if obj not in done:
                try:
                    obj.do_handshake()
                    done.add(obj)
                except ssl.SSLWantReadError:
                    pass
        server_in.write(client_out.read())
        client_in.write(server_out.read())
        if len(done) == 2:
            break
    assert len(done) == 2
    # Consume TLS 1.3 tickets before inspecting the unrelated certificate store.
    with pytest.raises(ssl.SSLWantReadError):
        client.read(1)
    return client


class TestSSL:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (" STDLIB_SSL ", ["ssl", "rtls", "utls"]),
            ("boring-ssl", ["utls", "rtls", "ssl"]),
            ("AWS_LC", ["rtls", "utls", "ssl"]),
            ("  ", ["rtls", "utls", "ssl"]),
        ],
    )
    def test_backend_preference(self, value: str, expected: list[str]) -> None:
        from urllib3.contrib.anytls._backend import _parse_pref

        assert _parse_pref(value) == expected

    def test_unknown_backend_preference_warns(self) -> None:
        from urllib3.contrib.anytls._backend import _parse_pref

        with pytest.warns(UserWarning, match="Unknown value.*not-a-backend"):
            assert _parse_pref("not-a-backend") == ["rtls", "utls", "ssl"]

    def test_context_cache_requires_lock_and_distinguishes_dict_options(self) -> None:
        cache = ssl_._CacheableSSLContext()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with pytest.raises(OSError, match="WITH lock"):
            cache.get()
        with pytest.raises(OSError, match="WITH lock"):
            cache.save(context)
        with cache.lock({"option": "one"}):
            cache.save(context)
        with cache.lock({"option": "one"}):
            assert cache.get() is context
        with cache.lock({"option": "two"}):
            assert cache.get() is None

    @pytest.mark.parametrize("fallback", [False, True])
    def test_cert_store_error_queue_isolation(
        self, malformed_ca: str, fallback: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if ssl_._get_clear_error() is None:
            pytest.skip("stdlib OpenSSL error queue is not accessible")
        from urllib3.contrib.imcc._ctypes import _OpenSSL

        lib = _OpenSSL()
        contexts = [ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT) for _ in range(5)]
        for ctx in contexts:
            ctx.load_verify_locations(cadata=malformed_ca)

        def drain() -> list[int]:
            errors = []
            while True:
                error = lib.ERR_get_error()
                if not error:
                    break
                errors.append(error)
            return errors

        lib.ERR_clear_error()
        try:
            # Error-stack depth differs between OpenSSL versions.
            contexts[0].cert_store_stats()
            contexts[1].cert_store_stats()
            expected = drain()
            assert len(expected) >= 2
            contexts[2].cert_store_stats()
            contexts[3].cert_store_stats()
            if fallback:
                monkeypatch.setattr(ssl_, "_get_clear_error", lambda: None)
            ssl_._cert_store_stats(contexts[4])
            # The worker cannot consume the caller's errors or add its own.
            # The inline path clears every entry, including its own errors.
            assert drain() == (expected if fallback else [])
        finally:
            lib.ERR_clear_error()

    @pytest.mark.parametrize("method", ["cert_store_stats", "get_ca_certs"])
    @pytest.mark.parametrize("fallback", [False, True])
    def test_cert_store_inspection_does_not_poison_tls_read(
        self,
        malformed_ca: str,
        tls_reader: ssl.SSLObject,
        method: str,
        fallback: bool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clear = ssl_._get_clear_error()
        if not fallback and clear is None:
            pytest.skip("stdlib OpenSSL error queue is not accessible")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cadata=malformed_ca)
        if fallback:
            monkeypatch.setattr(ssl_, "_get_clear_error", lambda: None)
        try:
            if method == "cert_store_stats":
                assert ssl_._cert_store_stats(ctx) == {
                    "x509": 1,
                    "x509_ca": 0,
                    "crl": 0,
                }
            else:
                assert ssl_._get_ca_certs(ctx) == []
            with pytest.raises(ssl.SSLWantReadError):
                tls_reader.read(1)
        finally:
            if clear is not None:
                clear()

    @pytest.mark.parametrize("fallback", [False, True])
    @pytest.mark.parametrize("raises", [False, True])
    def test_cert_store_inspection_thread_and_exception(
        self, fallback: bool, raises: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        caller = threading.current_thread()
        threads = []
        expected = {"x509_ca": 2}
        error = NotImplementedError("inspection unavailable")
        clear = mock.Mock()
        monkeypatch.setattr(
            ssl_, "_get_clear_error", lambda: None if fallback else clear
        )

        def inspect() -> dict[str, int]:
            threads.append(threading.current_thread())
            if raises:
                raise error
            return expected

        monkeypatch.setattr(ctx, "cert_store_stats", inspect)
        if raises:
            with pytest.raises(NotImplementedError) as caught:
                ssl_._cert_store_stats(ctx)
            assert caught.value is error
        else:
            assert ssl_._cert_store_stats(ctx) is expected
        assert len(threads) == 1
        if fallback:
            assert threads[0] is not caller
            assert not threads[0].is_alive()
            clear.assert_not_called()
        else:
            assert threads[0] is caller
            clear.assert_called_once_with()

    @pytest.mark.parametrize("backend", ["rtls", "utls"])
    def test_alternative_cert_store_bypasses_workaround(
        self, backend: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from urllib3.contrib import anytls

        module = getattr(anytls, backend)
        if module is None:
            pytest.skip(f"{backend} is not installed")
        ctx = module.SSLContext(module.PROTOCOL_TLS_CLIENT)
        lookup = mock.Mock(side_effect=AssertionError("must not probe ctypes"))
        monkeypatch.setattr(ssl_, "_get_clear_error", lookup)
        assert ssl_._cert_store_stats(ctx) == ctx.cert_store_stats()
        assert ssl_._get_ca_certs(ctx) == ctx.get_ca_certs(binary_form=True)
        lookup.assert_not_called()

    def test_cert_store_results_are_not_cached(self) -> None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        assert ssl_._cert_store_stats(ctx)["x509_ca"] == 0
        assert ssl_._get_ca_certs(ctx) == []
        ca = trustme.CA()
        ca.configure_trust(ctx)
        assert ssl_._cert_store_stats(ctx)["x509_ca"] == 1
        assert len(ssl_._get_ca_certs(ctx)) == 1

    def test_cert_store_unavailable_binding_is_cached(self) -> None:
        from io import UnsupportedOperation
        from urllib3.contrib.imcc import _ctypes

        ssl_._get_clear_error.cache_clear()
        try:
            with mock.patch.object(
                _ctypes, "_OpenSSL", side_effect=UnsupportedOperation("unavailable")
            ) as factory:
                assert ssl_._get_clear_error() is None
                assert ssl_._get_clear_error() is None
                factory.assert_called_once_with()
        finally:
            ssl_._get_clear_error.cache_clear()

    @pytest.fixture(autouse=True)
    def _clear_ssl_context_cache(self) -> typing.Iterator[None]:
        from urllib3.util.ssl_ import _SSLContextCache

        _SSLContextCache.clear()
        try:
            yield
        finally:
            _SSLContextCache.clear()

    @pytest.mark.parametrize(
        "addr",
        [
            # IPv6
            "::1",
            "::",
            "FE80::8939:7684:D84b:a5A4%251",
            # IPv4
            "127.0.0.1",
            "8.8.8.8",
            b"127.0.0.1",
            # IPv6 w/ Zone IDs
            "FE80::8939:7684:D84b:a5A4%251",
            b"FE80::8939:7684:D84b:a5A4%251",
            "FE80::8939:7684:D84b:a5A4%19",
            b"FE80::8939:7684:D84b:a5A4%19",
        ],
    )
    def test_is_ipaddress_true(self, addr: bytes | str) -> None:
        assert ssl_.is_ipaddress(addr)

    @pytest.mark.parametrize(
        "addr",
        [
            "www.python.org",
            b"www.python.org",
            "v2.sg.media-imdb.com",
            b"v2.sg.media-imdb.com",
        ],
    )
    def test_is_ipaddress_false(self, addr: bytes | str) -> None:
        assert not ssl_.is_ipaddress(addr)

    def test_create_urllib3_context_set_ciphers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ciphers = "ECDH+AESGCM:ECDH+CHACHA20"
        context = mock.create_autospec(ssl_.SSLContext)
        context.set_ciphers = mock.Mock()
        context.options = 0
        monkeypatch.setattr(ssl_, "SSLContext", lambda *_, **__: context)

        assert ssl_.create_urllib3_context(ciphers=ciphers) is context

        assert context.set_ciphers.call_count == 1
        assert context.set_ciphers.call_args == mock.call(ciphers)

    def test_create_urllib3_no_context(self) -> None:
        with mock.patch("urllib3.util.ssl_.SSLContext", None):
            with pytest.raises(TypeError):
                ssl_.create_urllib3_context()

    def test_wrap_socket_given_context_no_load_default_certs(self) -> None:
        context = mock.create_autospec(ssl_.SSLContext)
        context.load_default_certs = mock.Mock()
        context.set_ciphers = mock.Mock()
        context.cert_store_stats = mock.Mock(return_value={"x509_ca": 5})

        sock = mock.Mock()
        ssl_.ssl_wrap_socket(sock, ssl_context=context, use_recommended_ciphers=True)

        context.load_default_certs.assert_not_called()
        context.set_ciphers.assert_not_called()

    def test_wrap_socket_given_ca_certs_no_load_default_certs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = mock.create_autospec(ssl_.SSLContext)
        context.load_default_certs = mock.Mock()
        context.options = 0

        monkeypatch.setattr(ssl_, "SSLContext", lambda *_, **__: context)

        sock = mock.Mock()
        ssl_.ssl_wrap_socket(sock, ca_certs="/tmp/fake-file-1")

        context.load_default_certs.assert_not_called()
        context.load_verify_locations.assert_called_with("/tmp/fake-file-1", None, None)

    def test_wrap_socket_default_loads_default_certs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = mock.create_autospec(ssl_.SSLContext)
        context.load_default_certs = mock.Mock()
        context.options = 0

        monkeypatch.setattr(ssl_, "SSLContext", lambda *_, **__: context)

        sock = mock.Mock()
        ssl_.ssl_wrap_socket(sock)

        context.load_default_certs.assert_called_with()

    def test_wrap_socket_no_ssltransport(self) -> None:
        with mock.patch("urllib3.util.ssl_.SSLTransport", None):
            with pytest.raises(ProxySchemeUnsupported):
                sock = mock.Mock()
                ssl_.ssl_wrap_socket(sock, tls_in_tls=True)

    @pytest.mark.parametrize(
        ["pha", "expected_pha"], [(None, None), (False, True), (True, True)]
    )
    def test_create_urllib3_context_pha(
        self,
        monkeypatch: pytest.MonkeyPatch,
        pha: bool | None,
        expected_pha: bool | None,
    ) -> None:
        context = mock.create_autospec(ssl_.SSLContext)
        context.set_ciphers = mock.Mock()
        context.options = 0
        context.post_handshake_auth = pha
        monkeypatch.setattr(ssl_, "SSLContext", lambda *_, **__: context)

        assert ssl_.create_urllib3_context() is context

        assert context.post_handshake_auth == expected_pha

    def test_create_urllib3_context_default_ciphers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = mock.create_autospec(ssl_.SSLContext)
        context.set_ciphers = mock.Mock()
        context.options = 0

        monkeypatch.setattr(ssl_, "SSLContext", lambda *_, **__: context)
        monkeypatch.setattr(ssl_, "IS_NONSTDLIB", False)

        ssl_.create_urllib3_context()

        if ssl_.SUPPORT_MIN_MAX_TLS_VERSION:
            context.set_ciphers.assert_called_once_with(MOZ_INTERMEDIATE_CIPHERS)
        else:
            context.set_ciphers.assert_not_called()

        context.set_ciphers.reset_mock()
        ssl_.create_urllib3_context(use_recommended_ciphers=False)
        context.set_ciphers.assert_not_called()

    @pytest.mark.parametrize(
        "kwargs",
        [
            {
                "ssl_version": ssl.PROTOCOL_TLSv1,
                "ssl_minimum_version": ssl.TLSVersion.MINIMUM_SUPPORTED,
            },
            {
                "ssl_version": ssl.PROTOCOL_TLSv1,
                "ssl_maximum_version": ssl.TLSVersion.TLSv1,
            },
            {
                "ssl_version": ssl.PROTOCOL_TLSv1,
                "ssl_minimum_version": ssl.TLSVersion.MINIMUM_SUPPORTED,
                "ssl_maximum_version": ssl.TLSVersion.MAXIMUM_SUPPORTED,
            },
        ],
    )
    def test_create_urllib3_context_ssl_version_and_ssl_min_max_version_errors(
        self, kwargs: dict[str, typing.Any]
    ) -> None:
        with pytest.raises(ValueError) as e:
            ssl_.create_urllib3_context(**kwargs)

        assert str(e.value) == (
            "Can't specify both 'ssl_version' and either 'ssl_minimum_version' or 'ssl_maximum_version'"
        )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {
                "ssl_version": ssl.PROTOCOL_TLS,
                "ssl_minimum_version": ssl.TLSVersion.MINIMUM_SUPPORTED,
            },
            {
                "ssl_version": ssl.PROTOCOL_TLS_CLIENT,
                "ssl_minimum_version": ssl.TLSVersion.MINIMUM_SUPPORTED,
            },
            {
                "ssl_version": None,
                "ssl_minimum_version": ssl.TLSVersion.MINIMUM_SUPPORTED,
            },
        ],
    )
    def test_create_urllib3_context_ssl_version_and_ssl_min_max_version_no_warning(
        self, kwargs: dict[str, typing.Any]
    ) -> None:
        ssl_.create_urllib3_context(**kwargs)

    def test_assert_fingerprint_raises_exception_on_none_cert(self) -> None:
        with pytest.raises(SSLError):
            ssl_.assert_fingerprint(
                cert=None, fingerprint="55:39:BF:70:05:12:43:FA:1F:D1:BF:4E:E8:1B:07:1D"
            )

    @pytest.mark.parametrize(
        "fingerprint",
        [
            "g" * 32,
            "g" * 40,
            "g" * 64,
            "a" * 39 + "g",
            "GG:" * 19 + "GG",
        ],
    )
    def test_assert_fingerprint_raises_sslerror_on_non_hexadecimal(
        self, fingerprint: str
    ) -> None:
        with pytest.raises(SSLError):
            ssl_.assert_fingerprint(b"certificate", fingerprint)

    def test_create_urllib3_context_force_stdlib_backend(self) -> None:
        ctx = ssl_.create_urllib3_context(ssl_backend="ssl")
        # The stdlib backend must always produce a stdlib ``ssl.SSLContext``.
        assert type(ctx).__module__ == "ssl"

    @pytest.mark.parametrize("backend", ["rtls", "utls"])
    def test_create_urllib3_context_force_nonstdlib_backend(self, backend: str) -> None:
        from urllib3.contrib import anytls

        module = getattr(anytls, backend)
        if module is None:
            pytest.skip(f"{backend} backend is not installed")

        ctx = ssl_.create_urllib3_context(ssl_backend=backend)  # type: ignore[arg-type]
        assert type(ctx).__module__.split(".")[0] == backend

    def test_create_urllib3_context_force_unavailable_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from urllib3.contrib import anytls

        monkeypatch.setattr(anytls, "utls", None, raising=False)
        with pytest.raises(SSLError, match="utls.* is not available"):
            ssl_.create_urllib3_context(ssl_backend="utls")

    def test_ssl_backend_changes_ssl_context_cache_key(self) -> None:
        # Forcing different backends must not collide in the SSLContext cache.
        from urllib3.util.ssl_ import _SSLContextCache

        keys = []
        for backend in ("ssl", "rtls", "utls", None):
            args = [None] * 16 + [backend]
            with _SSLContextCache.lock(*args):
                keys.append(_SSLContextCache._cursor)
        assert len(set(keys)) == len(keys)

    def test_anytls_getattr_unknown_attribute(self) -> None:
        from urllib3.contrib import anytls

        with pytest.raises(AttributeError):
            anytls.does_not_exist


@pytest.mark.parametrize("disabled_version", ["TLSv1_2", "TLSv1_3"])
@pytest.mark.filterwarnings(
    "ignore:ssl.OP_NO_SSL.*options are deprecated:DeprecationWarning"
)
def test_context_conversion_preserves_disabled_versions(disabled_version: str) -> None:
    from urllib3.contrib.anytls import IS_NONSTDLIB

    if not IS_NONSTDLIB:
        pytest.skip("Requires an alternative TLS backend")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.options |= getattr(ssl, "OP_NO_" + disabled_version)
    converted = ssl_.convert_ssl_ctx_nonstdlib(context)
    assert converted is not context
    assert not converted.check_hostname
    assert converted.verify_mode == active_ssl.CERT_REQUIRED
    assert int(converted.options) & int(
        getattr(active_ssl, "OP_NO_" + disabled_version)
    )
    assert converted.cert_store_stats()["x509_ca"] > 0


def test_tls12_context_disables_quic() -> None:
    context = ssl_.create_urllib3_context()
    context.maximum_version = active_ssl.TLSVersion.TLSv1_2
    assert not ssl_.is_capable_for_quic(context, None)


def test_unknown_legacy_tls_version_uses_maximum_supported() -> None:
    assert (
        ssl_.resolve_ssl_version(-123, mitigate_tls_version=True)
        == active_ssl.TLSVersion.MAXIMUM_SUPPORTED
    )


def test_client_chain_ignores_trailing_comment() -> None:
    from urllib3.contrib.imcc._ctypes import _split_client_cert

    ca = trustme.CA()
    cert = ca.issue_cert("client.example").cert_chain_pems[0].bytes()
    assert _split_client_cert(cert + b"# trailing comment\n") == [cert]


@pytest.mark.asyncio
async def test_async_tls_missing_ca_file(tmp_path: typing.Any) -> None:
    from urllib3.contrib.ssa import AsyncSocket
    from urllib3.util._async.ssl_ import ssl_wrap_socket

    sock = AsyncSocket()
    try:
        with pytest.raises(SSLError) as caught:
            await ssl_wrap_socket(sock, ca_certs=str(tmp_path / "missing.pem"))
        assert isinstance(caught.value.__cause__, OSError)
    finally:
        sock.close()


@pytest.mark.asyncio
async def test_async_in_memory_certificate_unavailable_warns(
    san_server: typing.Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib3 import AsyncPoolManager
    from urllib3.contrib import imcc
    from urllib3.contrib.anytls import IS_NONSTDLIB

    if IS_NONSTDLIB:
        pytest.skip("Alternative backends support in-memory certificates natively")
    client = trustme.CA().issue_cert("client.example")
    monkeypatch.setattr(imcc, "SUPPORTED_METHODS", [])
    async with AsyncPoolManager(
        ca_certs=san_server.ca_certs,
        cert_data=client.cert_chain_pems[0].bytes(),
        key_data=client.private_key_pem.bytes(),
        timeout=5,
        retries=False,
    ) as manager:
        with pytest.warns(UserWarning, match="in-memory.*unsupported on your platform"):
            response = await manager.request("GET", san_server.base_url)
        assert response.status == 200
