from __future__ import annotations

import errno
import socket
import sys
from typing import Any

import pytest

from urllib3._constant import UDP_LINUX_GRO, UDP_LINUX_SEGMENT
from urllib3.contrib.ssa import _gro


def test_sync_datagram_send_propagates_connection_error() -> None:
    failure = ConnectionRefusedError(errno.ECONNREFUSED, "injected send failure")

    class Sender(socket.socket):
        fail_send = True

        def send(self, data: Any, *args: Any, **kwargs: Any) -> int:
            if self.fail_send:
                raise failure
            return super().send(data, *args, **kwargs)

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver, Sender(
        socket.AF_INET, socket.SOCK_DGRAM
    ) as sender:
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(5)
        sender.connect(receiver.getsockname())
        with pytest.raises(ConnectionRefusedError) as caught:
            _gro.sync_send_dgram(sender, b"failed")
        assert caught.value is failure
        sender.fail_send = False
        _gro.sync_send_dgram(sender, b"after")
        assert receiver.recv(1024) == b"after"


@pytest.mark.skipif(not hasattr(socket.socket, "sendmsg"), reason="POSIX sendmsg")
@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize("failure", [errno.EMSGSIZE, errno.ECONNREFUSED])
def test_sync_gso_send_failure_preserves_datagrams(grouped: bool, failure: int) -> None:
    class Sender(socket.socket):
        failed = False

        def sendmsg(self, buffers: Any, *args: Any, **kwargs: Any) -> int:
            # Force the batch fallback while individual sends remain real UDP.
            raise OSError(errno.EMSGSIZE, "injected rejected batch")

        def send(self, data: Any, *args: Any, **kwargs: Any) -> int:
            if not self.failed:
                self.failed = True
                raise OSError(failure, "injected datagram failure")
            return super().send(data, *args, **kwargs)

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver, Sender(
        socket.AF_INET, socket.SOCK_DGRAM
    ) as sender:
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(5)
        sender.connect(receiver.getsockname())
        messages = [b"one", b"two"] if grouped else [b"one"]
        if failure == errno.ECONNREFUSED:
            with pytest.raises(ConnectionRefusedError):
                _gro.sync_sendmsg_gso(sender, messages)
        else:
            _gro.sync_sendmsg_gso(sender, messages)
            if grouped:
                assert receiver.recv(1024) == b"two"
        _gro.sync_sendmsg_gso(sender, [])
        _gro.sync_sendmsg_gso(sender, [b"after"])
        assert receiver.recv(1024) == b"after"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux UDP GRO metadata")
@pytest.mark.parametrize("mode", ["truncated", "ancillary-truncated", "single"])
@pytest.mark.usefixtures("python_udp_transport")
def test_kernel_gro_receive_boundaries(
    mode: str,
) -> None:
    class Receiver(socket.socket):
        saw_control = False

        def recvmsg(
            self, bufsize: int, ancbufsize: int = 0, *args: Any, **kwargs: Any
        ) -> Any:
            data, ancillary, flags, address = super().recvmsg(
                1 if mode == "truncated" else bufsize,
                0 if mode == "ancillary-truncated" else ancbufsize,
                *args,
                **kwargs,
            )
            self.saw_control = bool(ancillary or flags)
            return data, ancillary, flags, address

    with Receiver(socket.AF_INET, socket.SOCK_DGRAM) as receiver, socket.socket(
        socket.AF_INET, socket.SOCK_DGRAM
    ) as sender:
        try:
            receiver.setsockopt(_gro._SOL_UDP, UDP_LINUX_GRO, 1)
            sender.setsockopt(_gro._SOL_UDP, UDP_LINUX_SEGMENT, 0)
        except OSError:
            pytest.skip("Kernel does not support UDP segmentation offload")
        receiver.bind(("127.0.0.1", 0))
        sender.connect(receiver.getsockname())
        payload = b"segment"
        # Two segments force ancillary truncation when recvmsg is given no
        # ancillary buffer space. A single segment may have no GRO metadata.
        packet = payload * (2 if mode == "ancillary-truncated" else 1)
        receiver.settimeout(5)
        sender.sendmsg(
            [packet],
            [
                (
                    _gro._SOL_UDP,
                    UDP_LINUX_SEGMENT,
                    _gro._UINT16.pack(len(payload)),
                )
            ],
        )
        if mode == "truncated":
            with pytest.raises(OSError, match="payload truncated"):
                _gro.sync_recv_gro(receiver, 1024)
        else:
            assert _gro.sync_recv_gro(receiver, 1024) == packet
        if mode != "single":
            assert receiver.saw_control, "Kernel did not deliver truncation flags"


@pytest.mark.skipif(not hasattr(socket.socket, "sendmsg"), reason="POSIX sendmsg")
def test_sync_gso_empty_datagram_is_delivered() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender, socket.socket(
        socket.AF_INET, socket.SOCK_DGRAM
    ) as receiver:
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(5)
        sender.connect(receiver.getsockname())
        _gro.sync_sendmsg_gso(sender, [b""])
        assert receiver.recv(1) == b""
