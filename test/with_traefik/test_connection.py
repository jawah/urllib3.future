from __future__ import annotations

import pytest

from urllib3 import HttpVersion
from urllib3.connection import HTTPSConnection
from urllib3.exceptions import ResponseNotReady
from urllib3.util import create_urllib3_context

from . import TraefikTestCase


class TestConnection(TraefikTestCase):
    @pytest.mark.usefixtures("requires_http3")
    def test_h3_probe_after_close(self) -> None:
        conn = HTTPSConnection(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver.new(),
        )

        conn.request("GET", "/get")

        resp = conn.getresponse()

        assert resp.version == 20

        conn.close()

        conn.connect()

        conn.request("GET", "/get")

        resp = conn.getresponse()

        assert resp.version == 30

        conn.close()

    def test_h2_svn_conserved(self) -> None:
        conn = HTTPSConnection(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            disabled_svn={HttpVersion.h3},
            resolver=self.test_resolver.new(),
        )

        conn.request("GET", "/get")

        resp = conn.getresponse()

        assert resp.version == 20

        conn.close()

        assert hasattr(conn, "_http_vsn") and conn._http_vsn == 20

        conn.connect()

        conn.request("GET", "/get")

        resp = conn.getresponse()

        assert resp.version == 20

    def test_getresponse_not_ready(self) -> None:
        conn = HTTPSConnection(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver.new(),
        )

        conn.close()

        with pytest.raises(ResponseNotReady):
            conn.getresponse()

    @pytest.mark.usefixtures("requires_http3")
    def test_quic_cache_capable(self) -> None:
        quic_cache_resumption: dict[tuple[str, int], tuple[str, int] | None] = {
            (self.host, self.https_port): ("", self.https_port)
        }

        conn = HTTPSConnection(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            preemptive_quic_cache=quic_cache_resumption,
            resolver=self.test_resolver.new(),
        )

        conn.request("GET", "/get")
        resp = conn.getresponse()

        assert resp.status == 200
        assert resp.version == 30

    def test_quic_cache_capable_but_disabled(self) -> None:
        quic_cache_resumption: dict[tuple[str, int], tuple[str, int] | None] = {
            (self.host, self.https_port): ("", self.https_port)
        }

        conn = HTTPSConnection(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            preemptive_quic_cache=quic_cache_resumption,
            disabled_svn={HttpVersion.h3},
            resolver=self.test_resolver.new(),
        )

        conn.request("GET", "/get")
        resp = conn.getresponse()

        assert resp.status == 200
        assert resp.version == 20

    @pytest.mark.usefixtures("requires_http3")
    def test_quic_cache_explicit_not_capable(self) -> None:
        quic_cache_resumption: dict[tuple[str, int], tuple[str, int] | None] = {
            (self.host, self.https_port): None
        }

        conn = HTTPSConnection(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            preemptive_quic_cache=quic_cache_resumption,
            resolver=self.test_resolver.new(),
        )

        conn.request("GET", "/get")
        resp = conn.getresponse()

        assert resp.status == 200
        assert resp.version == 20

    @pytest.mark.usefixtures("requires_http3")
    def test_quic_cache_implicit_not_capable(self) -> None:
        quic_cache_resumption: dict[tuple[str, int], tuple[str, int] | None] = dict()

        conn = HTTPSConnection(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            preemptive_quic_cache=quic_cache_resumption,
            resolver=self.test_resolver.new(),
        )

        conn.request("GET", "/get")
        resp = conn.getresponse()

        assert resp.status == 200
        assert resp.version == 20

        assert len(quic_cache_resumption.keys()) == 1
        assert (self.host, self.https_port) in quic_cache_resumption

    @pytest.mark.usefixtures("requires_http3")
    def test_quic_extract_ssl_ctx_ca_root(self) -> None:
        quic_cache_resumption: dict[tuple[str, int], tuple[str, int] | None] = {
            (self.host, self.https_port): ("", self.https_port)
        }

        ctx = create_urllib3_context()
        ctx.load_verify_locations(cafile=self.ca_authority)

        conn = HTTPSConnection(
            self.host,
            self.https_port,
            ssl_context=ctx,
            preemptive_quic_cache=quic_cache_resumption,
            resolver=self.test_resolver.new(),
        )

        conn.request("GET", "/get")
        resp = conn.getresponse()

        assert conn._HfaceBackend__custom_tls_settings is not None  # type: ignore
        detect_ctx_fallback = conn._HfaceBackend__custom_tls_settings.cadata  # type: ignore

        assert detect_ctx_fallback is not None
        assert isinstance(detect_ctx_fallback, bytes)
        assert self.ca_authority is not None

        with open(self.ca_authority, "rb") as fp:
            assert fp.read() in detect_ctx_fallback

        assert resp.status == 200
        assert resp.version == 30
