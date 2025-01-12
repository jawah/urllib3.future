from __future__ import annotations

import io
import os
import platform
from base64 import b64decode
from io import BytesIO

import pytest

from urllib3 import AsyncHTTPSConnectionPool
from urllib3.backend._async.hface import _HAS_HTTP3_SUPPORT  # type: ignore

from .. import TraefikTestCase


@pytest.mark.asyncio
class TestPostBody(TraefikTestCase):
    async def test_overrule_unicode_content_length(self) -> None:
        async with AsyncHTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver,
        ) as p:
            resp = await p.request(
                "POST", "/post", body="🚀", headers={"Content-Length": "1"}
            )

            assert resp.status == 200
            assert "Content-Length" in (await resp.json())["headers"]
            assert (await resp.json())["headers"]["Content-Length"][0] == "4"

    async def test_overrule_unicode_content_length_with_bytes_content_type(
        self,
    ) -> None:
        async with AsyncHTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver,
        ) as p:
            resp = await p.request(
                "POST",
                "/post",
                body="🚀",
                headers={"Content-Length": "1", "Content-Type": b"plain/text"},  # type: ignore[dict-item]
            )

            assert resp.status == 200
            assert "Content-Length" in (await resp.json())["headers"]
            assert "Content-Type" in (await resp.json())["headers"]
            assert (await resp.json())["headers"]["Content-Type"][
                0
            ] == "plain/text; charset=utf-8"
            assert (await resp.json())["headers"]["Content-Length"][0] == "4"

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
    async def test_h2n3_data(self, method: str, body: bytes | str | BytesIO) -> None:
        async with AsyncHTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver,
        ) as p:
            for i in range(3):
                if isinstance(body, BytesIO):
                    body.seek(0, 0)

                resp = await p.request(method, f"/{method.lower()}", body=body)

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

                echo_data_from_httpbin = (await resp.json())["data"]
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
    async def test_h2n3_form_field(self, method: str, fields: dict[str, str]) -> None:
        async with AsyncHTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver,
        ) as p:
            for i in range(2):
                resp = await p.request(method, f"/{method.lower()}", fields=fields)

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

                payload = await resp.json()

                for key in fields:
                    assert key in payload["form"]
                    assert fields[key] in payload["form"][key]

    async def test_upload_track_progress(self) -> None:
        progress_track = []

        async def track(
            total_sent: int,
            content_length: int | None,
            is_completed: bool,
            any_error: bool,
        ) -> None:
            nonlocal progress_track
            progress_track.append((total_sent, content_length, is_completed, any_error))

        async with AsyncHTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver,
        ) as p:
            await p.urlopen("POST", "/post", body=b"foo" * 16800, on_upload_body=track)

        assert len(progress_track) > 0
        assert progress_track[-1][-2] is True
        assert progress_track[0][1] == 16800 * 3
        assert progress_track[-1][0] == 16800 * 3
        assert progress_track[0][0] <= 16800 * 3

    async def test_upload_track_progress_no_content_length(self) -> None:
        progress_track = []

        async def track(
            total_sent: int,
            content_length: int | None,
            is_completed: bool,
            any_error: bool,
        ) -> None:
            nonlocal progress_track
            progress_track.append((total_sent, content_length, is_completed, any_error))

        async with AsyncHTTPSConnectionPool(
            self.host,
            self.https_port,
            ca_certs=self.ca_authority,
            resolver=self.test_async_resolver,
        ) as p:
            await p.urlopen(
                "POST", "/post", body=io.BytesIO(b"foo" * 16800), on_upload_body=track
            )

        assert len(progress_track) > 0
        assert progress_track[-1][-2] is True
        assert progress_track[0][1] is None
        assert progress_track[-1][0] == 16800 * 3
        assert progress_track[0][0] <= 16800 * 3
