from __future__ import annotations

import io
import socket
import typing

import pytest

from urllib3.exceptions import (
    UnrewindableBodyError,
)
from urllib3.util.request import arewind_body


class TestUtil:
    @pytest.mark.asyncio
    async def test_arewind_body_no_seek(self) -> None:
        class NoSeek(io.StringIO):
            seek = None  # type: ignore[assignment]

        with pytest.raises(UnrewindableBodyError):
            await arewind_body(NoSeek(), body_pos=2)

    @pytest.mark.asyncio
    async def test_arewind_nonseekable_file(
        self, async_file_wrapper: typing.Callable[[typing.BinaryIO], typing.Any]
    ) -> None:
        sender, receiver = socket.socketpair()
        with sender, receiver, receiver.makefile("rb") as file:
            with pytest.raises(UnrewindableBodyError, match="An error occurred") as exc:
                await arewind_body(async_file_wrapper(file), 0)
            assert isinstance(exc.value.__cause__, OSError)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("position", [None, "0"])
    async def test_arewind_body_invalid_position(
        self,
        position: typing.Any,
        async_file_wrapper: typing.Callable[[typing.BinaryIO], typing.Any],
    ) -> None:
        with io.BytesIO(b"body") as file:
            with pytest.raises(ValueError, match="body_pos must be of type integer"):
                await arewind_body(async_file_wrapper(file), position)
            assert file.tell() == 0
