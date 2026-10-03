from __future__ import annotations

import socket

import io
import os
import platform
import typing
from base64 import b64decode
from io import BytesIO

import pytest

from urllib3 import HTTPSConnectionPool
from urllib3.backend import HttpVersion
from urllib3.backend import hface
from urllib3.backend.hface import _HAS_HTTP3_SUPPORT
from urllib3.contrib.ssa._gro import GenericSegmentOffloadUnsupported

from . import TraefikTestCase


class TestPostBody(TraefikTestCase):
    def test_overrule_unicode_content_length(self) -> None:
        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
        ) as p:
            resp = p.request(
                "POST", "/post", body="🚀", headers={"Content-Length": "1"}
            )

            assert resp.status == 200
            assert "Content-Length" in resp.json()["headers"]
            assert (
                resp.json()["headers"]["Content-Type"][0] == "text/plain; charset=utf-8"
            )
            assert resp.json()["headers"]["Content-Length"][0] == "4"

    def test_overrule_unicode_content_length_with_bytes_content_type(
        self,
    ) -> None:
        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
        ) as p:
            resp = p.request(
                "POST",
                "/post",
                body="🚀",
                headers={"Content-Length": "1", "Content-Type": b"text/plain"},  # type: ignore[dict-item]
            )

            assert resp.status == 200
            assert "Content-Length" in resp.json()["headers"]
            assert "Content-Type" in resp.json()["headers"]
            assert resp.json()["headers"]["Content-Type"][0] == "text/plain"
            assert resp.json()["headers"]["Content-Length"][0] == "4"

    @pytest.mark.parametrize(
        "method",
        [
            "POST",
            "PUT",
            "PATCH",
        ],
    )
    @pytest.mark.parametrize(
        "body",
        [
            "This is a rocket 🚀!",
            "This is a rocket 🚀!".encode(),
            BytesIO(b"foo" * 100),
            b"x" * 10,
            BytesIO(b"x" * 64),
            b"foo\r\n",  # meant to verify that function unpack_chunk() in method send() work in edge cases
            BytesIO(b"foo\r\n"),
            BytesIO(
                b"foo" * 1200
            ),  # meant to verify that we respect quic max packet size (outgoing)
        ],
    )
    def test_h2n3_data(self, method: str, body: bytes | str | BytesIO) -> None:
        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
        ) as p:
            # first will be done in HTTP/2
            # two others using HTTP/3
            for i in range(3):
                if isinstance(body, BytesIO):
                    body.seek(0, 0)

                resp = p.request(method, f"/{method.lower()}", body=body)

                assert resp.status == 200

                if _HAS_HTTP3_SUPPORT():
                    # colima is our only way to test HTTP/2 and HTTP/3 in GHA runners
                    # its known to have flaky behaviors. We can lose the connection easily...
                    # and our automatic downgrade to HTTP/2 makes the following assert
                    # problematic!
                    if (
                        os.environ.get("CI") is not None
                        and platform.system() == "Darwin"
                    ):
                        assert resp.version in {20, 30}
                    else:
                        assert resp.version == (20 if i == 0 else 30)
                else:
                    assert resp.version == 20

                echo_data_from_httpbin = resp.json()["data"]
                need_b64_decode = echo_data_from_httpbin.startswith(
                    "data:application/octet-stream;base64,"
                )

                if need_b64_decode:
                    echo_data_from_httpbin = b64decode(echo_data_from_httpbin[37:])

                payload_seen_by_server: bytes = (
                    echo_data_from_httpbin
                    if isinstance(echo_data_from_httpbin, bytes)
                    else echo_data_from_httpbin.encode()
                )

                if isinstance(body, str):
                    assert payload_seen_by_server == body.encode("utf-8"), (
                        f"HTTP/{resp.version / 10} POST body failure: str"
                    )
                elif isinstance(body, bytes):
                    assert payload_seen_by_server == body, (
                        f"HTTP/{resp.version / 10} POST body failure: bytes"
                    )
                else:
                    body.seek(0, 0)
                    assert payload_seen_by_server == body.read(), (
                        f"HTTP/{resp.version / 10} POST body failure: BytesIO"
                    )

    @pytest.mark.parametrize(
        "method",
        [
            "POST",
            "PUT",
            "PATCH",
        ],
    )
    @pytest.mark.parametrize(
        "fields",
        [
            {"a": "c", "d": "f", "foo": "bar"},
            {"bobaaz": "really confident"},
            {"z": "", "o": "klm"},
        ],
    )
    def test_h2n3_form_field(self, method: str, fields: dict[str, str]) -> None:
        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
        ) as p:
            for i in range(2):
                resp = p.request(method, f"/{method.lower()}", fields=fields)

                assert resp.status == 200
                if _HAS_HTTP3_SUPPORT():
                    # colima is our only way to test HTTP/2 and HTTP/3 in GHA runners
                    # its known to have flaky behaviors. We can lose the connection easily...
                    # and our automatic downgrade to HTTP/2 makes the following assert
                    # problematic!
                    if (
                        os.environ.get("CI") is not None
                        and platform.system() == "Darwin"
                    ):
                        assert resp.version in {20, 30}
                    else:
                        assert resp.version == (20 if i == 0 else 30)
                else:
                    assert resp.version == 20

                payload = resp.json()

                for key in fields:
                    assert key in payload["form"]
                    assert fields[key] in payload["form"][key]

    @pytest.mark.parametrize("target_http", [20, 30])
    def test_upload_body_chunking(
        self, target_http: int, upload_body: tuple[typing.Any, bytes]
    ) -> None:
        if target_http == 30 and not _HAS_HTTP3_SUPPORT():
            pytest.skip("Test requires HTTP/3 support")
        disabled_svn = {
            20: {HttpVersion.h11, HttpVersion.h3},
            30: {HttpVersion.h11, HttpVersion.h2},
        }[target_http]
        body, expected = upload_body
        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
            disabled_svn=disabled_svn,
            blocksize=4096,
            timeout=10,
            retries=False,
        ) as pool:
            resp = pool.request(
                "POST",
                "/post",
                body=body,
                headers={"Content-Type": "text/plain; charset=utf-8"},
            )
            assert resp.status == 200
            assert resp.version == target_http
            assert resp.json()["data"].encode() == expected

    def test_gso_rejection_preserves_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if not _HAS_HTTP3_SUPPORT():
            pytest.skip("HTTP/3 requires qh3")
        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
            disabled_svn={HttpVersion.h11, HttpVersion.h2},
            timeout=10,
            retries=False,
            background_watch_delay=None,
        ) as pool:
            assert pool.request("GET", "/get").version == 30
            assert pool.pool is not None
            with pool.pool.borrow() as conn:
                if not conn._dgram_gso_enabled:
                    pytest.skip("UDP segmentation offload unavailable")
            rejected = []

            def reject(sock: socket.socket, datagrams: list[bytes]) -> None:
                rejected.append(len(datagrams))
                raise GenericSegmentOffloadUnsupported()

            monkeypatch.setattr(hface, "sync_sendmsg_gso", reject)
            payload = "0123456789abcdef" * 8192
            response = pool.request("POST", "/post", body=payload)
            assert response.json()["data"] == payload
            assert response.status == 200 and response.version == 30
            assert len(rejected) == 1 and rejected[0] > 1
            with pool.pool.borrow() as conn:
                assert not conn._dgram_gso_enabled
            assert pool.request("GET", "/get").status == 200
            assert len(rejected) == 1

    def test_upload_track_progress(self) -> None:
        progress_track = []

        def track(
            total_sent: int,
            content_length: int | None,
            is_completed: bool,
            any_error: bool,
        ) -> None:
            nonlocal progress_track
            progress_track.append((total_sent, content_length, is_completed, any_error))

        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
        ) as p:
            p.urlopen("POST", "/post", body=b"foo" * 16800, on_upload_body=track)

        assert len(progress_track) > 0
        assert progress_track[-1][-2] is True
        assert progress_track[0][1] == 16800 * 3
        assert progress_track[-1][0] == 16800 * 3
        assert progress_track[0][0] <= 16800 * 3

    def test_upload_track_progress_no_content_length(self) -> None:
        progress_track = []

        def track(
            total_sent: int,
            content_length: int | None,
            is_completed: bool,
            any_error: bool,
        ) -> None:
            nonlocal progress_track
            progress_track.append((total_sent, content_length, is_completed, any_error))

        with HTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_resolver,
        ) as p:
            p.urlopen(
                "POST", "/post", body=io.BytesIO(b"foo" * 16800), on_upload_body=track
            )

        assert len(progress_track) > 0
        assert progress_track[-1][-2] is True
        assert progress_track[0][1] is None
        assert progress_track[-1][0] == 16800 * 3
        assert progress_track[0][0] <= 16800 * 3
