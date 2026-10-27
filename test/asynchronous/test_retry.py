from __future__ import annotations

import pytest

from urllib3._async.response import AsyncHTTPResponse
from urllib3.response import HTTPResponse
from urllib3.util.retry import Retry


class TestRetry:
    @pytest.mark.asyncio
    async def test_async_retry_after_preserves_sync_override(self) -> None:
        class CustomRetry(Retry):
            def get_retry_after(self, response: HTTPResponse) -> float | None:
                return super().get_retry_after(response)

        response = AsyncHTTPResponse(status=503, headers={"Retry-After": "2"})
        assert await CustomRetry().async_get_retry_after(response) == 2

    @pytest.mark.asyncio
    async def test_async_retry_after_hook(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class BodyRetry(Retry):
            async def async_get_retry_after(
                self, response: AsyncHTTPResponse
            ) -> float | None:
                return float((await response.json())["delay"])

        sleeps = []

        async def sleep(delay: float) -> None:
            sleeps.append(delay)

        monkeypatch.setattr("urllib3.util.retry.asyncio.sleep", sleep)
        response = AsyncHTTPResponse(status=503, body=b'{"delay": 2.5}')
        await BodyRetry().async_sleep(response)
        assert sleeps == [2.5]
        await BodyRetry(respect_retry_after_header=False).async_sleep(response)
        assert sleeps == [2.5]
