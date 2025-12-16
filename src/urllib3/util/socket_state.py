from __future__ import annotations

import errno
import sys
import socket
import struct
import typing

if typing.TYPE_CHECKING:
    from ..contrib.ssa import AsyncSocket
    from ..util.ssltransport import SSLTransport

IS_NT = sys.platform in {"win32", "cygwin", "msys"}
IS_DARWIN_OR_BSD = not IS_NT and (sys.platform == "darwin" or "bsd" in sys.platform)
IS_LINUX = not IS_DARWIN_OR_BSD and sys.platform == "linux"
SOCKET_CLOSED_ERRNOS: frozenset[int] = frozenset(
    filter(
        None,
        (
            getattr(errno, "EBADF", None),
            getattr(errno, "ENOTSOCK", None),
            getattr(errno, "EINVAL", None),
            getattr(errno, "ENOTCONN", None),
        ),
    )
)

# Of course, Windows don't have any nice shortcut
# through getsockopt, why make it simple when a
# hard way exist? Let's contact the winapi directly.
if IS_NT:
    import ctypes
    import ctypes.wintypes

    class WindowsTcpInfo(ctypes.Structure):
        """
        WindowsTcpInfo structure (https://learn.microsoft.com/en-us/windows/desktop/api/mstcpip/ns-mstcpip-tcp_info_v0)

        Minimum supported client: (Windows 10, version 1703 // Windows Server 2016)
        """

        _fields_ = [
            ("State", ctypes.c_int),
            ("Mss", ctypes.wintypes.ULONG),
            ("ConnectionTimeMs", ctypes.c_uint64),
            ("TimestampsEnabled", ctypes.wintypes.BOOLEAN),
            ("RttUs", ctypes.wintypes.ULONG),
            ("MinRttUs", ctypes.wintypes.ULONG),
            ("BytesInFlight", ctypes.wintypes.ULONG),
            ("Cwnd", ctypes.wintypes.ULONG),
            ("SndWnd", ctypes.wintypes.ULONG),
            ("RcvWnd", ctypes.wintypes.ULONG),
            ("RcvBuf", ctypes.wintypes.ULONG),
            ("BytesOut", ctypes.c_uint64),
            ("BytesIn", ctypes.c_uint64),
            ("BytesReordered", ctypes.wintypes.ULONG),
            ("BytesRetrans", ctypes.wintypes.ULONG),
            ("FastRetrans", ctypes.wintypes.ULONG),
            ("DupAcksIn", ctypes.wintypes.ULONG),
            ("TimeoutEpisodes", ctypes.wintypes.ULONG),
            ("SynRetrans", ctypes.c_uint8),
        ]

    try:
        WSAIoctl_Fn = ctypes.windll.ws2_32.WSAIoctl  # type: ignore[attr-defined]
    except AttributeError:  # Defensive: very old Windows distribution
        WSAIoctl_Fn = None
    else:
        WSAIoctl_Fn.argtypes = [
            ctypes.c_void_p,  # [in]  SOCKET  s
            ctypes.wintypes.DWORD,  # [in]  DWORD   SIO_TCP_INFO
            ctypes.c_void_p,  # [in]  LPVOID  lpvInBuffer
            ctypes.wintypes.DWORD,  # [in]  DWORD   cbInBuffer
            ctypes.c_void_p,  # [out] LPVOID  lpvOutBuffer
            ctypes.wintypes.DWORD,  # [in]  DWORD   cbOutBuffer
            ctypes.POINTER(ctypes.wintypes.DWORD),  # [out] LPWORD  lpcbBytesReturned
            ctypes.c_void_p,  # [in]  LPWSAOVERLAPPED lpOverlapped
            ctypes.c_void_p,  # [in]  LPWSAOVERLAPPED_COMPLETION_ROUTINE lpCompletionRoutine
        ]
        WSAIoctl_Fn.restype = ctypes.c_int  # int

    SIO_TCP_INFO = ctypes.wintypes.DWORD(
        1 << 31  # IOC_IN
        | 1 << 30  # IOC_OUT
        | 3 << 27  # IOC_VENDOR
        | 39
    )


def is_established(sock: socket.socket | AsyncSocket | SSLTransport) -> bool:
    """
    Determine by best effort if the socket is closed
    without ever attempting to read from it.
    This works by trying to get the TCP current status.
    """
    if sock.fileno() == -1:
        return False

    # catch earlier the most catastrophic states
    # this pre-check avoid wasting time on TCP probing
    try:
        err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if err != 0:
            return False
    except OSError:
        return False

    # Well... If we're on UDP (or anything else),
    if sock.type is not socket.SOCK_STREAM:
        return True

    if IS_DARWIN_OR_BSD:
        if sys.platform == "darwin":
            TCP_CONNECTION_INFO = getattr(socket, "TCP_CONNECTION_INFO", 0x106)
        else:
            TCP_CONNECTION_INFO = getattr(socket, "TCP_INFO", 11)

        try:
            info = sock.getsockopt(socket.IPPROTO_TCP, TCP_CONNECTION_INFO, 1024)
        except OSError as e:
            # EBADF, ENOTSOCK, EINVAL are expected for invalid/closed sockets
            # Other errors might indicate real issues
            if e.errno in SOCKET_CLOSED_ERRNOS:
                return False
            return True

        state: int = struct.unpack("B", info[0:1])[0]

        # macOS/BSD TCP states:
        # TCPS_CLOSED      = 0
        # TCPS_LISTEN      = 1
        # TCPS_SYN_SENT    = 2
        # TCPS_SYN_RCVD    = 3
        # TCPS_ESTABLISHED = 4
        # TCPS_CLOSE_WAIT  = 5
        # TCPS_FIN_WAIT_1  = 6
        # TCPS_CLOSING     = 7
        # TCPS_LAST_ACK    = 8
        # TCPS_FIN_WAIT_2  = 9
        # TCPS_TIME_WAIT   = 10
        return state == 4
    elif IS_LINUX:
        TCP_INFO = getattr(socket, "TCP_INFO", 11)

        try:
            info = sock.getsockopt(socket.IPPROTO_TCP, TCP_INFO, 1024)
        except OSError:
            return True

        state = struct.unpack("B", info[0:1])[0]

        # linux header
        # enum {
        #     TCP_ESTABLISHED = 1,
        #     TCP_SYN_SENT    = 2,
        #     TCP_SYN_RECV    = 3,
        #     TCP_FIN_WAIT1   = 4,
        #     TCP_FIN_WAIT2   = 5,
        #     TCP_TIME_WAIT   = 6,
        #     TCP_CLOSE       = 7,
        #     TCP_CLOSE_WAIT  = 8,
        #     TCP_LAST_ACK    = 9,
        #     TCP_LISTEN      = 10,
        #     TCP_CLOSING     = 11
        # };
        return state == 1
    elif IS_NT:
        if WSAIoctl_Fn is None:
            return True

        sockfd = ctypes.c_void_p(sock.fileno())

        info_version = ctypes.wintypes.DWORD(0)
        tcp_info = WindowsTcpInfo()
        bytes_returned = ctypes.wintypes.DWORD(0)

        ioctl_return_code = WSAIoctl_Fn(
            sockfd,
            SIO_TCP_INFO,
            ctypes.pointer(info_version),
            ctypes.wintypes.DWORD(ctypes.sizeof(info_version)),
            ctypes.pointer(tcp_info),
            ctypes.wintypes.DWORD(ctypes.sizeof(tcp_info)),
            ctypes.pointer(bytes_returned),
            None,
            None,
        )

        if ioctl_return_code == 0:
            # https://learn.microsoft.com/en-us/windows/win32/api/mstcpip/ne-mstcpip-tcpstate
            # 0 = Closed
            # 1 = Listen
            # 2 = Syn Sent
            # 3 = Syn Rcvd
            # 4 = Established
            # 5 = Fin Wait 1
            # 6 = Fin Wait 2
            # 7 = Close Wait
            # 8 = Closing
            # 9 = Last Ack
            # 10 = Time Wait
            # 11 = Max?
            return tcp_info.State == 4  # type: ignore[no-any-return]

    return True
