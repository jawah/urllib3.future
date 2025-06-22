from __future__ import annotations

import os.path
import platform
import shutil
import ssl
import sys
import tempfile
import warnings
from pathlib import Path
from test import (
    LONG_TIMEOUT,
    SHORT_TIMEOUT,
    TARPIT_HOST,
    requires_network,
    resolvesLocalhostFQDN,
)
from test.conftest import ServerConfig
from unittest import mock

import pytest
import trustme

import urllib3.util as util
import urllib3.util.ssl_
from dummyserver.server import (
    DEFAULT_CA,
    DEFAULT_CA_KEY,
    DEFAULT_CERTS,
    encrypt_key_pem,
)
from dummyserver.testcase import HTTPSDummyServerTestCase
from urllib3 import HTTPSConnectionPool
from urllib3.connection import HTTPSConnection, VerifiedHTTPSConnection
from urllib3.exceptions import (
    ConnectTimeoutError,
    InsecureRequestWarning,
    MaxRetryError,
    ProtocolError,
    SSLError,
)
from urllib3.util.ssl_match_hostname import CertificateError
from urllib3.util.timeout import Timeout

from .. import has_alpn

TLSv1_CERTS = DEFAULT_CERTS.copy()
TLSv1_CERTS["ssl_version"] = getattr(ssl, "PROTOCOL_TLSv1", None)

TLSv1_1_CERTS = DEFAULT_CERTS.copy()
TLSv1_1_CERTS["ssl_version"] = getattr(ssl, "PROTOCOL_TLSv1_1", None)

TLSv1_2_CERTS = DEFAULT_CERTS.copy()
TLSv1_2_CERTS["ssl_version"] = getattr(ssl, "PROTOCOL_TLSv1_2", None)

TLSv1_3_CERTS = DEFAULT_CERTS.copy()
TLSv1_3_CERTS["ssl_version"] = getattr(ssl, "PROTOCOL_TLS_CLIENT", None)


CLIENT_INTERMEDIATE_PEM = "client_intermediate.pem"
CLIENT_NO_INTERMEDIATE_PEM = "client_no_intermediate.pem"
CLIENT_INTERMEDIATE_KEY = "client_intermediate.key"
PASSWORD_CLIENT_KEYFILE = "client_password.key"
CLIENT_CERT = CLIENT_INTERMEDIATE_PEM


class TestHTTPS(HTTPSDummyServerTestCase):
    tls_protocol_name: str | None = None

    def tls_protocol_not_default(self) -> bool:
        return self.tls_protocol_name in {"TLSv1", "TLSv1.1"}

    def tls_version(self) -> ssl.TLSVersion:
        if self.tls_protocol_name is None:
            return pytest.skip("Skipping base test class")
        try:
            from ssl import TLSVersion
        except ImportError:
            return pytest.skip("ssl.TLSVersion isn't available")
        return TLSVersion[self.tls_protocol_name.replace(".", "_")]

    def ssl_version(self) -> int:
        if self.tls_protocol_name is None:
            return pytest.skip("Skipping base test class")
        attribute = f"PROTOCOL_{self.tls_protocol_name.replace('.', '_')}"
        ssl_version = getattr(ssl, attribute, None)
        if ssl_version is None:
            return pytest.skip(f"ssl.{attribute} isn't available")
        return ssl_version  # type: ignore[no-any-return]

    @classmethod
    def setup_class(cls) -> None:
        super().setup_class()

        cls.certs_dir = tempfile.mkdtemp()
        # Start from existing root CA as we don't want to change the server certificate yet
        with open(DEFAULT_CA, "rb") as crt, open(DEFAULT_CA_KEY, "rb") as key:
            root_ca = trustme.CA.from_pem(crt.read(), key.read())

        # Generate another CA to test verification failure
        bad_ca = trustme.CA()
        cls.bad_ca_path = os.path.join(cls.certs_dir, "ca_bad.pem")
        bad_ca.cert_pem.write_to_path(cls.bad_ca_path)

        # client cert chain
        intermediate_ca = root_ca.create_child_ca()
        cert = intermediate_ca.issue_cert("example.com")
        encrypted_key = encrypt_key_pem(cert.private_key_pem, b"letmein")

        cert.private_key_pem.write_to_path(
            os.path.join(cls.certs_dir, CLIENT_INTERMEDIATE_KEY)
        )
        encrypted_key.write_to_path(
            os.path.join(cls.certs_dir, PASSWORD_CLIENT_KEYFILE)
        )
        # Write the client cert and the intermediate CA
        client_cert = os.path.join(cls.certs_dir, CLIENT_INTERMEDIATE_PEM)
        cert.cert_chain_pems[0].write_to_path(client_cert)
        cert.cert_chain_pems[1].write_to_path(client_cert, append=True)
        # Write only the client cert
        cert.cert_chain_pems[0].write_to_path(
            os.path.join(cls.certs_dir, CLIENT_NO_INTERMEDIATE_PEM)
        )

    @classmethod
    def teardown_class(cls) -> None:
        super().teardown_class()

        shutil.rmtree(cls.certs_dir)

    def test_simple(self) -> None:
        with HTTPSConnectionPool(
            self.host,
            self.port,
            ca_certs=DEFAULT_CA,
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            r = https_pool.request("GET", "/")
            assert r.status == 200, r.data

    @resolvesLocalhostFQDN()
    def test_dotted_fqdn(self) -> None:
        with HTTPSConnectionPool(
            self.host + ".",
            self.port,
            ca_certs=DEFAULT_CA,
            ssl_minimum_version=self.tls_version(),
        ) as pool:
            r = pool.request("GET", "/")
            assert r.status == 200, r.data

    def test_client_intermediate(self) -> None:
        """Check that certificate chains work well with client certs

        We generate an intermediate CA from the root CA, and issue a client certificate
        from that intermediate CA. Since the server only knows about the root CA, we
        need to send it the certificate *and* the intermediate CA, so that it can check
        the whole chain.
        """
        with HTTPSConnectionPool(
            self.host,
            self.port,
            key_file=os.path.join(self.certs_dir, CLIENT_INTERMEDIATE_KEY),
            cert_file=os.path.join(self.certs_dir, CLIENT_INTERMEDIATE_PEM),
            ca_certs=DEFAULT_CA,
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            r = https_pool.request("GET", "/certificate")
            subject = r.json()
            assert subject["organizationalUnitName"].startswith("Testing cert")

    @pytest.mark.xfail(
        sys.implementation.name == "pypy"
        and (
            platform.python_version().startswith("3.11")
            or platform.python_version().startswith("3.10")
        ),
        reason="PyPy libffi does not implement _shm_open (probable bug)",
        strict=False,
    )
    def test_in_memory_client_intermediate(self) -> None:
        with open(os.path.join(self.certs_dir, CLIENT_INTERMEDIATE_KEY)) as fp_key_data:
            with open(
                os.path.join(self.certs_dir, CLIENT_INTERMEDIATE_PEM)
            ) as fp_cert_data:
                with HTTPSConnectionPool(
                    self.host,
                    self.port,
                    key_data=fp_key_data.read(),
                    cert_data=fp_cert_data.read(),
                    ca_certs=DEFAULT_CA,
                    ssl_minimum_version=self.tls_version(),
                    retries=False,
                ) as https_pool:
                    r = https_pool.request("GET", "/certificate")
                    subject = r.json()
                    assert subject["organizationalUnitName"].startswith("Testing cert")

    def test_client_no_intermediate(self) -> None:
        """Check that missing links in certificate chains indeed break

        The only difference with test_client_intermediate is that we don't send the
        intermediate CA to the server, only the client cert.
        """
        with HTTPSConnectionPool(
            self.host,
            self.port,
            cert_file=os.path.join(self.certs_dir, CLIENT_NO_INTERMEDIATE_PEM),
            key_file=os.path.join(self.certs_dir, CLIENT_INTERMEDIATE_KEY),
            ca_certs=DEFAULT_CA,
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            with pytest.raises((SSLError, ProtocolError)):
                https_pool.request("GET", "/certificate", retries=False)

    def test_client_key_password(self) -> None:
        with HTTPSConnectionPool(
            self.host,
            self.port,
            ca_certs=DEFAULT_CA,
            key_file=os.path.join(self.certs_dir, PASSWORD_CLIENT_KEYFILE),
            cert_file=os.path.join(self.certs_dir, CLIENT_CERT),
            key_password="letmein",
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            r = https_pool.request("GET", "/certificate")
            subject = r.json()
            assert subject["organizationalUnitName"].startswith("Testing cert")

    @pytest.mark.xfail(
        sys.implementation.name == "pypy"
        and (
            platform.python_version().startswith("3.11")
            or platform.python_version().startswith("3.10")
        ),
        reason="PyPy libffi does not implement _shm_open (probable bug)",
        strict=False,
    )
    def test_in_memory_client_key_password(self) -> None:
        with open(os.path.join(self.certs_dir, PASSWORD_CLIENT_KEYFILE)) as fp_key_data:
            with open(os.path.join(self.certs_dir, CLIENT_CERT)) as fp_cert_data:
                with HTTPSConnectionPool(
                    self.host,
                    self.port,
                    ca_certs=DEFAULT_CA,
                    key_data=fp_key_data.read(),
                    cert_data=fp_cert_data.read(),
                    key_password="letmein",
                    ssl_minimum_version=self.tls_version(),
                    retries=False,
                ) as https_pool:
                    r = https_pool.request("GET", "/certificate")
                    subject = r.json()
                    assert subject["organizationalUnitName"].startswith("Testing cert")

    def test_client_encrypted_key_requires_password(self) -> None:
        with HTTPSConnectionPool(
            self.host,
            self.port,
            key_file=os.path.join(self.certs_dir, PASSWORD_CLIENT_KEYFILE),
            cert_file=os.path.join(self.certs_dir, CLIENT_CERT),
            key_password=None,
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            with pytest.raises(MaxRetryError, match="password is required") as e:
                https_pool.request("GET", "/certificate")

            assert isinstance(e.value.reason, SSLError)

    def test_verified(self) -> None:
        # PyPy 3.10+ workaround raised warning about untrustworthy TLS protocols.
        if sys.implementation.name == "pypy":
            warnings.filterwarnings(
                "ignore", r"ssl.* is deprecated", DeprecationWarning
            )

        with HTTPSConnectionPool(
            self.host,
            self.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=DEFAULT_CA,
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            conn = https_pool._new_conn()
            assert conn.__class__ == VerifiedHTTPSConnection
            https_pool._put_conn(conn)

            with warnings.catch_warnings(record=True) as w:
                r = https_pool.request("GET", "/")
                assert r.status == 200

            assert [str(wm) for wm in w] == []

    def test_verified_with_context(self) -> None:
        ctx = util.ssl_.create_urllib3_context(
            cert_reqs=ssl.CERT_REQUIRED, ssl_minimum_version=self.tls_version()
        )
        ctx.load_verify_locations(cafile=DEFAULT_CA)
        with HTTPSConnectionPool(self.host, self.port, ssl_context=ctx) as https_pool:
            conn = https_pool._new_conn()
            assert conn.__class__ == VerifiedHTTPSConnection
            https_pool._put_conn(conn)

            with mock.patch("warnings.warn") as warn:
                r = https_pool.request("GET", "/")
                assert r.status == 200
                assert not warn.called, warn.call_args_list

    def test_context_combines_with_ca_certs(self) -> None:
        ctx = util.ssl_.create_urllib3_context(
            cert_reqs=ssl.CERT_REQUIRED, ssl_minimum_version=self.tls_version()
        )
        with HTTPSConnectionPool(
            self.host, self.port, ca_certs=DEFAULT_CA, ssl_context=ctx
        ) as https_pool:
            conn = https_pool._new_conn()
            assert conn.__class__ == VerifiedHTTPSConnection
            https_pool._put_conn(conn)

            with mock.patch("warnings.warn") as warn:
                r = https_pool.request("GET", "/")
                assert r.status == 200
                assert not warn.called, warn.call_args_list

    def test_ca_dir_verified(self, tmp_path: Path) -> None:
        # PyPy 3.10+ workaround raised warning about untrustworthy TLS protocols.
        if sys.implementation.name == "pypy":
            warnings.filterwarnings(
                "ignore", r"ssl.* is deprecated", DeprecationWarning
            )

        # OpenSSL looks up certificates by the hash for their name, see c_rehash
        # TODO infer the bytes using `cryptography.x509.Name.public_bytes`.
        # https://github.com/pyca/cryptography/pull/3236
        shutil.copyfile(DEFAULT_CA, str(tmp_path / "81deb5f7.0"))

        with HTTPSConnectionPool(
            self.host,
            self.port,
            cert_reqs="CERT_REQUIRED",
            ca_cert_dir=str(tmp_path),
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            conn = https_pool._new_conn()
            assert conn.__class__ == VerifiedHTTPSConnection
            https_pool._put_conn(conn)

            with warnings.catch_warnings(record=True) as w:
                r = https_pool.request("GET", "/")
                assert r.status == 200

            assert [str(wm) for wm in w] == []

    def test_invalid_common_name(self) -> None:
        with HTTPSConnectionPool(
            "127.0.0.1",
            self.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=DEFAULT_CA,
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            with pytest.raises(MaxRetryError) as e:
                https_pool.request("GET", "/", retries=0)
            assert isinstance(e.value.reason, SSLError)
            assert "doesn't match" in str(
                e.value.reason
            ) or "certificate verify failed" in str(e.value.reason)

    def test_verified_with_bad_ca_certs(self) -> None:
        # PyPy 3.10+ workaround raised warning about untrustworthy TLS protocols.
        if sys.implementation.name == "pypy":
            warnings.filterwarnings(
                "ignore", r"ssl.* is deprecated", DeprecationWarning
            )

        with HTTPSConnectionPool(
            self.host,
            self.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=self.bad_ca_path,
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            with pytest.raises(MaxRetryError) as e:
                https_pool.request("GET", "/")
            assert isinstance(e.value.reason, SSLError)
            assert (
                "certificate verify failed" in str(e.value.reason)
                # PyPy is more specific
                or "self signed certificate in certificate chain" in str(e.value.reason)
            ), f"Expected 'certificate verify failed', instead got: {e.value.reason!r}"

    def test_wrap_socket_failure_resource_leak(self) -> None:
        with HTTPSConnectionPool(
            self.host,
            self.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=self.bad_ca_path,
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            conn = https_pool._get_conn()
            try:
                with pytest.raises(ssl.SSLError):
                    conn.connect()

                assert conn.sock is not None
            finally:
                conn.close()

    def test_verified_without_ca_certs(self) -> None:
        # default is cert_reqs=None which is ssl.CERT_NONE
        with HTTPSConnectionPool(
            self.host,
            self.port,
            cert_reqs="CERT_REQUIRED",
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            with pytest.raises(MaxRetryError) as e:
                https_pool.request("GET", "/")
            assert isinstance(e.value.reason, SSLError)
            # there is a different error message depending on whether or
            # not pyopenssl is injected
            assert (
                "No root certificates specified" in str(e.value.reason)
                # PyPy is more specific
                or "self signed certificate in certificate chain" in str(e.value.reason)
                # PyPy sometimes uses all-caps here
                or "certificate verify failed" in str(e.value.reason).lower()
                or "invalid certificate chain" in str(e.value.reason)
            ), (
                "Expected 'No root certificates specified',  "
                "'certificate verify failed', or "
                "'invalid certificate chain', "
                "instead got: %r" % e.value.reason
            )

    def test_no_ssl(self) -> None:
        with HTTPSConnectionPool(self.host, self.port) as pool:
            pool.ConnectionCls = None  # type: ignore[assignment]
            with pytest.raises(ImportError):
                pool._new_conn()
            with pytest.raises(ImportError):
                pool.request("GET", "/", retries=0)

    def test_unverified_ssl(self) -> None:
        """Test that bare HTTPSConnection can connect, make requests"""
        with HTTPSConnectionPool(
            self.host,
            self.port,
            cert_reqs=ssl.CERT_NONE,
            ssl_minimum_version=self.tls_version(),
        ) as pool:
            with mock.patch("warnings.warn") as warn:
                r = pool.request("GET", "/")
                assert r.status == 200
                assert warn.called

                # Modern versions of Python, or systems using PyOpenSSL, only emit
                # the unverified warning. Older systems may also emit other
                # warnings, which we want to ignore here.
                calls = warn.call_args_list
                assert InsecureRequestWarning in [x[0][1] for x in calls]

    def test_ssl_unverified_with_ca_certs(self) -> None:
        # PyPy 3.10+ workaround raised warning about untrustworthy TLS protocols.
        if sys.implementation.name == "pypy":
            warnings.filterwarnings(
                "ignore", r"ssl.* is deprecated", DeprecationWarning
            )

        with HTTPSConnectionPool(
            self.host,
            self.port,
            cert_reqs="CERT_NONE",
            ca_certs=self.bad_ca_path,
            ssl_minimum_version=self.tls_version(),
        ) as pool:
            with mock.patch("warnings.warn") as warn:
                r = pool.request("GET", "/")
                assert r.status == 200
                assert warn.called

                # Modern versions of Python, or systems using PyOpenSSL, only emit
                # the unverified warning. Older systems may also emit other
                # warnings, which we want to ignore here.
                calls = warn.call_args_list

                assert any(c[0][1] == InsecureRequestWarning for c in calls)

    def test_assert_hostname_false(self) -> None:
        with HTTPSConnectionPool(
            "localhost",
            self.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=DEFAULT_CA,
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            https_pool.assert_hostname = False
            https_pool.request("GET", "/")

    def test_assert_specific_hostname(self) -> None:
        with HTTPSConnectionPool(
            "localhost",
            self.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=DEFAULT_CA,
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            https_pool.assert_hostname = "localhost"
            https_pool.request("GET", "/")

    def test_server_hostname(self) -> None:
        with HTTPSConnectionPool(
            "127.0.0.1",
            self.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=DEFAULT_CA,
            server_hostname="localhost",
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            conn = https_pool._new_conn()
            conn.request("GET", "/")

            # Assert the wrapping socket is using the passed-through SNI name.
            # pyopenssl doesn't let you pull the server_hostname back off the
            # socket, so only add this assertion if the attribute is there (i.e.
            # the python ssl module).
            if hasattr(conn.sock, "server_hostname"):
                assert conn.sock.server_hostname == "localhost"  # type: ignore[union-attr]

            conn.close()

    def test_assert_fingerprint_md5(self) -> None:
        with HTTPSConnectionPool(
            "localhost",
            self.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=DEFAULT_CA,
            assert_fingerprint=("55:39:BF:70:05:12:43:FA:1F:D1:BF:4E:E8:1B:07:1D"),
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            https_pool.request("GET", "/")

    def test_assert_fingerprint_sha1(self) -> None:
        with HTTPSConnectionPool(
            "localhost",
            self.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=DEFAULT_CA,
            assert_fingerprint=(
                "72:8B:55:4C:9A:FC:1E:88:A1:1C:AD:1B:B2:E7:CC:3E:DB:C8:F9:8A"
            ),
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            https_pool.request("GET", "/")

    def test_assert_fingerprint_sha256(self) -> None:
        with HTTPSConnectionPool(
            "localhost",
            self.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=DEFAULT_CA,
            assert_fingerprint=(
                "E3:59:8E:69:FF:C5:9F:C7:88:87:44:58:22:7F:90:8D:D9:BC:12:C4:90:79:D5:"
                "DC:A8:5D:4F:60:40:1E:A6:D2"
            ),
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            https_pool.request("GET", "/")

    def test_assert_invalid_fingerprint(self) -> None:
        def _test_request(pool: HTTPSConnectionPool) -> SSLError:
            with pytest.raises(MaxRetryError) as cm:
                pool.request("GET", "/", retries=0)
            assert isinstance(cm.value.reason, SSLError)
            return cm.value.reason

        with HTTPSConnectionPool(
            self.host,
            self.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=DEFAULT_CA,
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            https_pool.assert_fingerprint = (
                "AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA"
            )
            e = _test_request(https_pool)
            expected = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            got = "728b554c9afc1e88a11cad1bb2e7cc3edbc8f98a"
            assert (
                str(e)
                == f'Fingerprints did not match. Expected "{expected}", got "{got}"'
            )

            # Uneven length
            https_pool.assert_fingerprint = "AA:A"
            e = _test_request(https_pool)
            assert "Fingerprint of invalid length:" in str(e)

            # Invalid length
            https_pool.assert_fingerprint = "AA"
            e = _test_request(https_pool)
            assert "Fingerprint of invalid length:" in str(e)

    def test_verify_none_and_bad_fingerprint(self) -> None:
        with HTTPSConnectionPool(
            "127.0.0.1",
            self.port,
            cert_reqs="CERT_NONE",
            assert_hostname=False,
            assert_fingerprint=(
                "AA:8B:55:4C:9A:FC:1E:88:A1:1C:AD:1B:B2:E7:CC:3E:DB:C8:F9:8A"
            ),
        ) as https_pool:
            with pytest.raises(MaxRetryError) as cm:
                https_pool.request("GET", "/", retries=0)
            assert isinstance(cm.value.reason, SSLError)

    def test_verify_none_and_good_fingerprint(self) -> None:
        with HTTPSConnectionPool(
            "127.0.0.1",
            self.port,
            cert_reqs="CERT_NONE",
            assert_hostname=False,
            assert_fingerprint=(
                "72:8B:55:4C:9A:FC:1E:88:A1:1C:AD:1B:B2:E7:CC:3E:DB:C8:F9:8A"
            ),
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            https_pool.request("GET", "/")

    def test_good_fingerprint_and_hostname_mismatch(self) -> None:
        # This test doesn't run with SecureTransport because we don't turn off
        # hostname validation without turning off all validation, which this
        # test doesn't do (deliberately). We should revisit this if we make
        # new decisions.
        with HTTPSConnectionPool(
            "127.0.0.1",
            self.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=DEFAULT_CA,
            assert_fingerprint=(
                "72:8B:55:4C:9A:FC:1E:88:A1:1C:AD:1B:B2:E7:CC:3E:DB:C8:F9:8A"
            ),
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            https_pool.request("GET", "/")

    @requires_network()
    def test_https_timeout(self) -> None:
        timeout = Timeout(total=None, connect=SHORT_TIMEOUT)
        with HTTPSConnectionPool(
            TARPIT_HOST,
            self.port,
            timeout=timeout,
            retries=False,
            cert_reqs="CERT_REQUIRED",
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            with pytest.raises(ConnectTimeoutError):
                https_pool.request("GET", "/")

        timeout = Timeout(read=0.01)
        with HTTPSConnectionPool(
            self.host,
            self.port,
            timeout=timeout,
            retries=False,
            cert_reqs="CERT_REQUIRED",
            ca_certs=DEFAULT_CA,
            assert_fingerprint=(
                "72:8B:55:4C:9A:FC:1E:88:A1:1C:AD:1B:B2:E7:CC:3E:DB:C8:F9:8A"
            ),
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            # TODO This was removed in https://github.com/urllib3/urllib3/pull/703/files
            # We need to put something back or remove this block.
            pass

        timeout = Timeout(total=None)
        with HTTPSConnectionPool(
            self.host,
            self.port,
            timeout=timeout,
            cert_reqs="CERT_NONE",
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            with pytest.warns(InsecureRequestWarning):
                https_pool.request("GET", "/")

    def test_tunnel(self) -> None:
        """test the _tunnel behavior"""
        timeout = Timeout(total=None)
        with HTTPSConnectionPool(
            self.host,
            self.port,
            timeout=timeout,
            cert_reqs="CERT_NONE",
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            conn = https_pool._new_conn()
            try:
                conn.set_tunnel(self.host, self.port)
                with mock.patch.object(
                    conn, "_tunnel", create=True, return_value=None
                ) as conn_tunnel:
                    with pytest.warns(InsecureRequestWarning):
                        https_pool._make_request(conn, "GET", "/")
                conn_tunnel.assert_called_once_with()
            finally:
                conn.close()

    @requires_network()
    def test_enhanced_timeout(self) -> None:
        with HTTPSConnectionPool(
            TARPIT_HOST,
            self.port,
            timeout=Timeout(connect=SHORT_TIMEOUT),
            retries=False,
            cert_reqs="CERT_REQUIRED",
        ) as https_pool:
            conn = https_pool._new_conn()
            try:
                with pytest.raises(ConnectTimeoutError):
                    https_pool.request("GET", "/")
                with pytest.raises(ConnectTimeoutError):
                    https_pool._make_request(conn, "GET", "/")
            finally:
                conn.close()

        with HTTPSConnectionPool(
            TARPIT_HOST,
            self.port,
            timeout=Timeout(connect=LONG_TIMEOUT),
            retries=False,
            cert_reqs="CERT_REQUIRED",
        ) as https_pool:
            with pytest.raises(ConnectTimeoutError):
                https_pool.request("GET", "/", timeout=Timeout(connect=SHORT_TIMEOUT))

        with HTTPSConnectionPool(
            TARPIT_HOST,
            self.port,
            timeout=Timeout(total=None),
            retries=False,
            cert_reqs="CERT_REQUIRED",
        ) as https_pool:
            conn = https_pool._new_conn()
            try:
                with pytest.raises(ConnectTimeoutError):
                    https_pool.request(
                        "GET", "/", timeout=Timeout(total=None, connect=SHORT_TIMEOUT)
                    )
            finally:
                conn.close()

    def test_enhanced_ssl_connection(self) -> None:
        fingerprint = "72:8B:55:4C:9A:FC:1E:88:A1:1C:AD:1B:B2:E7:CC:3E:DB:C8:F9:8A"

        with HTTPSConnectionPool(
            self.host,
            self.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=DEFAULT_CA,
            assert_fingerprint=fingerprint,
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            r = https_pool.request("GET", "/")
            assert r.status == 200

    def test_ssl_correct_system_time(self) -> None:
        # PyPy 3.10+ workaround raised warning about untrustworthy TLS protocols.
        if sys.implementation.name == "pypy":
            warnings.filterwarnings(
                "ignore", r"ssl.* is deprecated", DeprecationWarning
            )

        with HTTPSConnectionPool(
            self.host,
            self.port,
            ca_certs=DEFAULT_CA,
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            https_pool.cert_reqs = "CERT_REQUIRED"
            https_pool.ca_certs = DEFAULT_CA

            w = self._request_without_resource_warnings("GET", "/")
            assert [] == w

    def _request_without_resource_warnings(
        self, method: str, url: str
    ) -> list[warnings.WarningMessage]:
        with warnings.catch_warnings(record=True) as w:
            # warnings.simplefilter("always")
            with HTTPSConnectionPool(
                self.host,
                self.port,
                ca_certs=DEFAULT_CA,
                ssl_minimum_version=self.tls_version(),
            ) as https_pool:
                https_pool.request(method, url)

        w = [x for x in w if not isinstance(x.message, ResourceWarning)]

        return w

    def test_set_ssl_version_to_tls_version(self) -> None:
        if self.tls_protocol_name is None:
            pytest.skip("Skipping base test class")

        with HTTPSConnectionPool(
            self.host, self.port, ca_certs=DEFAULT_CA
        ) as https_pool:
            https_pool.ssl_version = self.certs["ssl_version"]
            r = https_pool.request("GET", "/")
            assert r.status == 200, r.data

    @pytest.mark.parametrize("verify_mode", [ssl.CERT_NONE, ssl.CERT_REQUIRED])
    def test_set_cert_inherits_cert_reqs_from_ssl_context(
        self, verify_mode: int
    ) -> None:
        ssl_context = urllib3.util.ssl_.create_urllib3_context(cert_reqs=verify_mode)
        assert ssl_context.verify_mode == verify_mode

        conn = HTTPSConnection(self.host, self.port, ssl_context=ssl_context)

        assert conn.cert_reqs == verify_mode
        assert (
            conn.ssl_context is not None and conn.ssl_context.verify_mode == verify_mode
        )

    def test_tls_protocol_name_of_socket(self) -> None:
        if self.tls_protocol_name is None:
            pytest.skip("Skipping base test class")

        with HTTPSConnectionPool(
            self.host,
            self.port,
            ca_certs=DEFAULT_CA,
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            conn = https_pool._get_conn()
            try:
                conn.connect()
                if not hasattr(conn.sock, "version"):
                    pytest.skip("SSLSocket.version() not available")
                assert conn.sock.version() == self.tls_protocol_name  # type: ignore[union-attr]
            finally:
                conn.close()

    @pytest.mark.parametrize(
        "ssl_version", [None, ssl.PROTOCOL_TLS, ssl.PROTOCOL_TLS_CLIENT]
    )
    def test_ssl_version_with_protocol_tls_or_client_not_deprecated(
        self, ssl_version: int | None
    ) -> None:
        if self.tls_protocol_name is None:
            pytest.skip("Skipping base test class")
        if self.tls_protocol_not_default():
            pytest.skip(
                f"Skipping because '{self.tls_protocol_name}' isn't set by default"
            )

        with HTTPSConnectionPool(
            self.host, self.port, ca_certs=DEFAULT_CA, ssl_version=ssl_version
        ) as https_pool:
            conn = https_pool._get_conn()
            try:
                with warnings.catch_warnings(record=True) as w:
                    conn.connect()
            finally:
                conn.close()

        assert [str(wm) for wm in w if wm.category != ResourceWarning] == []

    def test_no_tls_version_deprecation_with_ssl_context(self) -> None:
        if self.tls_protocol_name is None:
            pytest.skip("Skipping base test class")

        ctx = util.ssl_.create_urllib3_context(ssl_minimum_version=self.tls_version())

        with HTTPSConnectionPool(
            self.host,
            self.port,
            ca_certs=DEFAULT_CA,
            ssl_context=ctx,
        ) as https_pool:
            conn = https_pool._get_conn()
            try:
                with warnings.catch_warnings(record=True) as w:
                    conn.connect()
            finally:
                conn.close()

        assert [str(wm) for wm in w if wm.category != ResourceWarning] == []

    def test_tls_version_maximum_and_minimum(self) -> None:
        if self.tls_protocol_name is None:
            pytest.skip("Skipping base test class")

        from ssl import TLSVersion

        min_max_versions = [
            (self.tls_version(), self.tls_version()),
            (TLSVersion.MINIMUM_SUPPORTED, self.tls_version()),
            (TLSVersion.MINIMUM_SUPPORTED, TLSVersion.MAXIMUM_SUPPORTED),
        ]

        for minimum_version, maximum_version in min_max_versions:
            with HTTPSConnectionPool(
                self.host,
                self.port,
                ca_certs=DEFAULT_CA,
                ssl_minimum_version=minimum_version,
                ssl_maximum_version=maximum_version,
            ) as https_pool:
                conn = https_pool._get_conn()
                try:
                    conn.connect()
                    assert conn.sock.version() == self.tls_protocol_name  # type: ignore[union-attr]
                finally:
                    conn.close()

    @pytest.mark.skipif(sys.version_info < (3, 8), reason="requires python 3.8+")
    def test_sslkeylogfile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if not hasattr(util.SSLContext, "keylog_filename"):
            pytest.skip("requires OpenSSL 1.1.1+")

        keylog_file = tmp_path / "keylogfile.txt"
        monkeypatch.setenv("SSLKEYLOGFILE", str(keylog_file))

        with HTTPSConnectionPool(
            self.host,
            self.port,
            ca_certs=DEFAULT_CA,
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            r = https_pool.request("GET", "/")
            assert r.status == 200, r.data
            assert keylog_file.is_file(), "keylogfile '%s' should exist" % str(
                keylog_file
            )
            assert keylog_file.read_text().startswith("# TLS secrets log file"), (
                "keylogfile '%s' should start with '# TLS secrets log file'"
                % str(keylog_file)
            )

    @pytest.mark.parametrize("sslkeylogfile", [None, ""])
    def test_sslkeylogfile_empty(
        self, monkeypatch: pytest.MonkeyPatch, sslkeylogfile: str | None
    ) -> None:
        # Assert that an HTTPS connection doesn't error out when given
        # no SSLKEYLOGFILE or an empty value (ie 'SSLKEYLOGFILE=')
        if sslkeylogfile is not None:
            monkeypatch.setenv("SSLKEYLOGFILE", sslkeylogfile)
        else:
            monkeypatch.delenv("SSLKEYLOGFILE", raising=False)
        with HTTPSConnectionPool(
            self.host,
            self.port,
            ca_certs=DEFAULT_CA,
            ssl_minimum_version=self.tls_version(),
        ) as pool:
            r = pool.request("GET", "/")
            assert r.status == 200, r.data

    def test_alpn_default(self) -> None:
        """Default ALPN protocols are sent by default."""
        if not has_alpn() or not has_alpn(ssl.SSLContext):
            pytest.skip("ALPN-support not available")
        with HTTPSConnectionPool(
            self.host,
            self.port,
            ca_certs=DEFAULT_CA,
            ssl_minimum_version=self.tls_version(),
        ) as pool:
            r = pool.request("GET", "/alpn_protocol", retries=0)
            assert r.status == 200
            assert r.data.decode("utf-8") == util.ALPN_PROTOCOLS[0]

    @pytest.mark.skipif(
        urllib3.util.ssl_.SUPPORT_MIN_MAX_TLS_VERSION is False,
        reason="Python built against restricted ssl library with one protocol supported",
    )
    def test_default_ssl_context_ssl_min_max_versions(self) -> None:
        ctx = urllib3.util.ssl_.create_urllib3_context()
        assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
        expected_maximum_version = ssl.SSLContext(
            ssl.PROTOCOL_TLS_CLIENT
        ).maximum_version
        assert ctx.maximum_version == expected_maximum_version

    @pytest.mark.skipif(
        urllib3.util.ssl_.SUPPORT_MIN_MAX_TLS_VERSION is False,
        reason="Python built against restricted ssl library with one protocol supported",
    )
    def test_ssl_context_ssl_version_uses_ssl_min_max_versions(self) -> None:
        ctx = urllib3.util.ssl_.create_urllib3_context(ssl_version=self.ssl_version())
        assert ctx.minimum_version == self.tls_version()
        assert ctx.maximum_version == self.tls_version()

    def test_assert_missing_hashfunc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fingerprint = "55:39:BF:70:05:12:43:FA:1F:D1:BF:4E:E8:1B:07:1D"
        with HTTPSConnectionPool(
            "localhost",
            self.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=DEFAULT_CA,
            assert_fingerprint=(fingerprint),
            ssl_minimum_version=self.tls_version(),
        ) as https_pool:
            digest_length = len(fingerprint.replace(":", "").lower())
            monkeypatch.setitem(urllib3.util.ssl_.HASHFUNC_MAP, digest_length, None)
            with pytest.raises(MaxRetryError) as cm:
                https_pool.request("GET", "/", retries=0)
            assert type(cm.value.reason) is SSLError
            assert (
                f"Hash function implementation unavailable for fingerprint length: {digest_length}"
                in str(cm.value.reason)
            )


@pytest.mark.usefixtures("requires_tlsv1")
class TestHTTPS_TLSv1(TestHTTPS):
    tls_protocol_name = "TLSv1"
    certs = TLSv1_CERTS


@pytest.mark.usefixtures("requires_tlsv1_1")
class TestHTTPS_TLSv1_1(TestHTTPS):
    tls_protocol_name = "TLSv1.1"
    certs = TLSv1_1_CERTS


@pytest.mark.usefixtures("requires_tlsv1_2")
class TestHTTPS_TLSv1_2(TestHTTPS):
    tls_protocol_name = "TLSv1.2"
    certs = TLSv1_2_CERTS


@pytest.mark.usefixtures("requires_tlsv1_3")
class TestHTTPS_TLSv1_3(TestHTTPS):
    tls_protocol_name = "TLSv1.3"
    certs = TLSv1_3_CERTS


class TestHTTPS_Hostname:
    def test_can_validate_san(self, san_server: ServerConfig) -> None:
        """Ensure that urllib3 can validate SANs with IP addresses in them."""
        with HTTPSConnectionPool(
            san_server.host,
            san_server.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=san_server.ca_certs,
        ) as https_pool:
            r = https_pool.request("GET", "/")
            assert r.status == 200

    def test_ensure_validation_chain_incomplete(
        self, broken_intermediate_server: ServerConfig
    ) -> None:
        with HTTPSConnectionPool(
            broken_intermediate_server.host,
            broken_intermediate_server.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=broken_intermediate_server.ca_certs,
            retries=False,
        ) as https_pool:
            with pytest.raises(SSLError):
                https_pool.request("GET", "/")

    def test_ensure_validation_chain_rebuilt(
        self, broken_intermediate_server: ServerConfig
    ) -> None:
        with HTTPSConnectionPool(
            broken_intermediate_server.host,
            broken_intermediate_server.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=broken_intermediate_server.ca_certs,
            ca_cert_data=broken_intermediate_server.intermediate,
            retries=False,
        ) as https_pool:
            r = https_pool.request("GET", "/")
            assert r.status == 200

    def test_ensure_validation_chain_missing_anchor(
        self, broken_intermediate_server: ServerConfig
    ) -> None:
        with HTTPSConnectionPool(
            broken_intermediate_server.host,
            broken_intermediate_server.port,
            cert_reqs="CERT_REQUIRED",
            ca_cert_data=broken_intermediate_server.intermediate,
            retries=False,
        ) as https_pool:
            with pytest.raises(SSLError, match="issuer"):
                https_pool.request("GET", "/")

        with HTTPSConnectionPool(
            broken_intermediate_server.host,
            broken_intermediate_server.port,
            cert_reqs="CERT_REQUIRED",
            ca_cert_data=broken_intermediate_server.intermediate,
            retries=False,
        ) as https_pool:
            with pytest.raises(SSLError, match="issuer"):
                https_pool.request("GET", "/")

        assert broken_intermediate_server.intermediate is not None

        with HTTPSConnectionPool(
            broken_intermediate_server.host,
            broken_intermediate_server.port,
            cert_reqs="CERT_REQUIRED",
            ca_cert_data=f"""-----BEGIN CERTIFICATE-----
MIIEkTCCA3mgAwIBAgIERWtQVDANBgkqhkiG9w0BAQUFADCBsDELMAkGA1UEBhMC
VVMxFjAUBgNVBAoTDUVudHJ1c3QsIEluYy4xOTA3BgNVBAsTMHd3dy5lbnRydXN0
Lm5ldC9DUFMgaXMgaW5jb3Jwb3JhdGVkIGJ5IHJlZmVyZW5jZTEfMB0GA1UECxMW
KGMpIDIwMDYgRW50cnVzdCwgSW5jLjEtMCsGA1UEAxMkRW50cnVzdCBSb290IENl
cnRpZmljYXRpb24gQXV0aG9yaXR5MB4XDTA2MTEyNzIwMjM0MloXDTI2MTEyNzIw
NTM0MlowgbAxCzAJBgNVBAYTAlVTMRYwFAYDVQQKEw1FbnRydXN0LCBJbmMuMTkw
NwYDVQQLEzB3d3cuZW50cnVzdC5uZXQvQ1BTIGlzIGluY29ycG9yYXRlZCBieSBy
ZWZlcmVuY2UxHzAdBgNVBAsTFihjKSAyMDA2IEVudHJ1c3QsIEluYy4xLTArBgNV
BAMTJEVudHJ1c3QgUm9vdCBDZXJ0aWZpY2F0aW9uIEF1dGhvcml0eTCCASIwDQYJ
KoZIhvcNAQEBBQADggEPADCCAQoCggEBALaVtkNC+sZtKm9I35RMOVcF7sN5EUFo
Nu3s/poBj6E4KPz3EEZmLk0eGrEaTsbRwJWIsMn/MYszA9u3g3s+IIRe7bJWKKf4
4LlAcTfFy0cOlypowCKVYhXbR9n10Cv/gkvJrT7eTNuQgFA/CYqEAOwwCj0Yzfv9
KlmaI5UXLEWeH25DeW0MXJj+SKfFI0dcXv1u5x609mhF0YaDW6KKjbHjKYD+JXGI
rb68j6xSlkuqUY3kEzEZ6E5Nn9uss2rVvDlUccp6en+Q3X0dgNmBu1kmwhH+5pPi
94DkZfs0Nw4pgHBNrziGLp5/V6+eF67rHMsoIV+2HNjnogQi+dPa2MsCAwEAAaOB
sDCBrTAOBgNVHQ8BAf8EBAMCAQYwDwYDVR0TAQH/BAUwAwEB/zArBgNVHRAEJDAi
gA8yMDA2MTEyNzIwMjM0MlqBDzIwMjYxMTI3MjA1MzQyWjAfBgNVHSMEGDAWgBRo
kORnpKZTgMeGZqTx90tD+4S9bTAdBgNVHQ4EFgQUaJDkZ6SmU4DHhmak8fdLQ/uE
vW0wHQYJKoZIhvZ9B0EABBAwDhsIVjcuMTo0LjADAgSQMA0GCSqGSIb3DQEBBQUA
A4IBAQCT1DCw1wMgKtD5Y+iRDAUgqV8ZyntyTtSx29CW+1RaGSwMCPeyvIWonX9t
O1KzKtvn1ISMY/YPyyYBkVBs9F8U4pN0wBOeMDpQ47RgxRzwIkSNcUesyBrJ6Zua
AGAT/3B+XxFNSRuzFVJ7yVTav52Vr2ua2J7p8eRDjeIRRDq/r72DQnNSi6q7pynP
9WQcCk3RvKqsnyrQ/39/2n3qse0wJcGE2jTSW3iDVuycNsMm4hH2Z0kdkquM++v/
eu6FSqdQgPCnXEqULl8FmTxSQeDNtGPPAUO6nIPcj2A781q0tHuu2guQOHXvgR1m
0vdXcDazv/wor3ElhVsT/h5/WrQ8
-----END CERTIFICATE-----
{broken_intermediate_server.intermediate.decode()}
""",
            retries=False,
        ) as https_pool:
            with pytest.raises(SSLError, match="issuer"):
                https_pool.request("GET", "/")

    def test_common_name_without_san_fails(self, no_san_server: ServerConfig) -> None:
        with HTTPSConnectionPool(
            no_san_server.host,
            no_san_server.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=no_san_server.ca_certs,
        ) as https_pool:
            with pytest.raises(
                MaxRetryError,
            ) as e:
                https_pool.request("GET", "/")
            assert "mismatch, certificate is not valid" in str(
                e.value
            ) or "no appropriate subjectAltName" in str(e.value)

    def test_common_name_without_san_with_different_common_name(
        self, no_san_server_with_different_commmon_name: ServerConfig
    ) -> None:
        ctx = urllib3.util.ssl_.create_urllib3_context()
        try:
            ctx.hostname_checks_common_name = True
        except AttributeError:
            pytest.skip("Couldn't set 'SSLContext.hostname_checks_common_name'")

        with HTTPSConnectionPool(
            no_san_server_with_different_commmon_name.host,
            no_san_server_with_different_commmon_name.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=no_san_server_with_different_commmon_name.ca_certs,
            ssl_context=ctx,
        ) as https_pool:
            with pytest.raises(MaxRetryError) as e:
                https_pool.request("GET", "/")
            assert "mismatch, certificate is not valid for 'localhost'" in str(
                e.value
            ) or "hostname 'localhost' doesn't match 'example.com'" in str(e.value)

    @pytest.mark.parametrize("use_assert_hostname", [True, False])
    def test_hostname_checks_common_name_respected(
        self, no_san_server: ServerConfig, use_assert_hostname: bool
    ) -> None:
        ctx = urllib3.util.ssl_.create_urllib3_context()
        if not hasattr(ctx, "hostname_checks_common_name"):
            pytest.skip("Test requires 'SSLContext.hostname_checks_common_name'")
        ctx.load_verify_locations(no_san_server.ca_certs)
        try:
            ctx.hostname_checks_common_name = True
        except AttributeError:
            pytest.skip("Couldn't set 'SSLContext.hostname_checks_common_name'")

        err: MaxRetryError | None
        try:
            with HTTPSConnectionPool(
                no_san_server.host,
                no_san_server.port,
                cert_reqs="CERT_REQUIRED",
                ssl_context=ctx,
                assert_hostname=no_san_server.host if use_assert_hostname else None,
            ) as https_pool:
                https_pool.request("GET", "/")
        except MaxRetryError as e:
            err = e
        else:
            err = None

        # commonName is only valid for DNS names, not IP addresses.
        if no_san_server.host == "localhost":
            assert err is None

        # IP addresses should fail for commonName.
        else:
            assert err is not None
            assert type(err.reason) == SSLError
            assert isinstance(
                err.reason.args[0], (ssl.SSLCertVerificationError, CertificateError)
            )

    def test_assert_hostname_invalid_san(
        self, no_localhost_san_server: ServerConfig
    ) -> None:
        """Ensure SAN errors are not raised while assert_hostname is false"""
        with HTTPSConnectionPool(
            no_localhost_san_server.host,
            no_localhost_san_server.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=no_localhost_san_server.ca_certs,
            assert_hostname=False,
        ) as https_pool:
            https_pool.request("GET", "/")

    def test_assert_hostname_invalid_cn(
        self, no_san_server_with_different_commmon_name: ServerConfig
    ) -> None:
        """Ensure CN errors are not raised while assert_hostname is false"""
        with HTTPSConnectionPool(
            no_san_server_with_different_commmon_name.host,
            no_san_server_with_different_commmon_name.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=no_san_server_with_different_commmon_name.ca_certs,
            assert_hostname=False,
        ) as https_pool:
            https_pool.request("GET", "/")


class TestHTTPS_IPV4SAN:
    def test_can_validate_ip_san(self, ipv4_san_server: ServerConfig) -> None:
        """Ensure that urllib3 can validate SANs with IP addresses in them."""
        with HTTPSConnectionPool(
            ipv4_san_server.host,
            ipv4_san_server.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=ipv4_san_server.ca_certs,
        ) as https_pool:
            r = https_pool.request("GET", "/")
            assert r.status == 200


class TestHTTPS_IPV6SAN:
    @pytest.mark.parametrize("host", ["::1", "[::1]"])
    def test_can_validate_ipv6_san(
        self, ipv6_san_server: ServerConfig, host: str
    ) -> None:
        """Ensure that urllib3 can validate SANs with IPv6 addresses in them."""
        with HTTPSConnectionPool(
            host,
            ipv6_san_server.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=ipv6_san_server.ca_certs,
        ) as https_pool:
            r = https_pool.request("GET", "/")
            assert r.status == 200
