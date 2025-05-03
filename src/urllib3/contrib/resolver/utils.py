from __future__ import annotations

import base64
import binascii
import socket
import struct
import typing

if typing.TYPE_CHECKING:

    class HttpsRecord(typing.TypedDict):
        priority: int
        target: str
        alpn: list[str]
        ipv4hint: list[str]
        ipv6hint: list[str]
        echconfig: list[str]


def inet4_ntoa(address: bytes) -> str:
    """
    Convert an IPv4 address from bytes to str.
    """
    if len(address) != 4:
        raise ValueError(
            f"IPv4 addresses are 4 bytes long, got {len(address)} byte(s) instead"
        )

    return "%u.%u.%u.%u" % (address[0], address[1], address[2], address[3])


def inet6_ntoa(address: bytes) -> str:
    """
    Convert an IPv6 address from bytes to str.
    """
    if len(address) != 16:
        raise ValueError(
            f"IPv6 addresses are 16 bytes long, got {len(address)} byte(s) instead"
        )

    hex = binascii.hexlify(address)
    chunks = []

    i = 0
    length = len(hex)

    while i < length:
        chunk = hex[i : i + 4].decode().lstrip("0") or "0"
        chunks.append(chunk)
        i += 4

    # Compress the longest subsequence of 0-value chunks to ::
    best_start = 0
    best_len = 0
    start = -1
    last_was_zero = False

    for i in range(8):
        if chunks[i] != "0":
            if last_was_zero:
                end = i
                current_len = end - start
                if current_len > best_len:
                    best_start = start
                    best_len = current_len
                last_was_zero = False
        elif not last_was_zero:
            start = i
            last_was_zero = True
    if last_was_zero:
        end = 8
        current_len = end - start
        if current_len > best_len:
            best_start = start
            best_len = current_len
    if best_len > 1:
        if best_start == 0 and (best_len == 6 or best_len == 5 and chunks[5] == "ffff"):
            # We have an embedded IPv4 address
            if best_len == 6:
                prefix = "::"
            else:
                prefix = "::ffff:"
            thex = prefix + inet4_ntoa(address[12:])
        else:
            thex = (
                ":".join(chunks[:best_start])
                + "::"
                + ":".join(chunks[best_start + best_len :])
            )
    else:
        thex = ":".join(chunks)

    return thex


def packet_fragment(payload: bytes, *identifiers: bytes) -> tuple[bytes, ...]:
    results = []

    offset = 0

    start_packet_idx = []
    lead_identifier = None

    for identifier in identifiers:
        idx = payload[:12].find(identifier)

        if idx == -1:
            continue

        if idx != 0:
            offset = idx

        start_packet_idx.append(idx - offset)

        lead_identifier = identifier
        break

    for identifier in identifiers:
        if identifier == lead_identifier:
            continue

        if offset == 0:
            idx = payload.find(b"\x02" + identifier)
        else:
            idx = payload.find(identifier)

        if idx == -1:
            continue

        start_packet_idx.append(idx - offset)

    if not start_packet_idx:
        raise ValueError(
            "no identifiable dns message emerged from given payload. "
            "this should not happen at all. networking issue?"
        )

    if len(start_packet_idx) == 1:
        return (payload,)

    start_packet_idx = sorted(start_packet_idx)

    previous_idx = None

    for idx in start_packet_idx:
        if previous_idx is None:
            previous_idx = idx
            continue
        results.append(payload[previous_idx:idx])
        previous_idx = idx

    results.append(payload[previous_idx:])

    return tuple(results)


def is_ipv4(addr: str) -> bool:
    try:
        socket.inet_aton(addr)
        return True
    except OSError:
        return False


def is_ipv6(addr: str) -> bool:
    try:
        socket.inet_pton(socket.AF_INET6, addr)
        return True
    except OSError:
        return False


def validate_length_of(hostname: str) -> None:
    """RFC 1035 impose a limit on a domain name length. We verify it there."""
    if len(hostname.strip(".")) > 253:
        raise UnicodeError("hostname to resolve exceed 253 characters")
    elif any([len(_) > 63 for _ in hostname.split(".")]):
        raise UnicodeError("at least one label to resolve exceed 63 characters")


def rfc1035_should_read(payload: bytes) -> bool:
    if not payload:
        return False
    if len(payload) <= 2:
        return True

    cursor = payload

    while True:
        expected_size: int = struct.unpack("!H", cursor[:2])[0]

        if len(cursor[2:]) == expected_size:
            return False
        elif len(cursor[2:]) < expected_size:
            return True

        cursor = cursor[2 + expected_size :]


def rfc1035_unpack(payload: bytes) -> tuple[bytes, ...]:
    cursor = payload
    packets = []

    while cursor:
        expected_size: int = struct.unpack("!H", cursor[:2])[0]

        packets.append(cursor[2 : 2 + expected_size])
        cursor = cursor[2 + expected_size :]

    return tuple(packets)


def rfc1035_pack(message: bytes) -> bytes:
    return struct.pack("!H", len(message)) + message


def read_name(data: bytes, offset: int) -> tuple[str, int]:
    """
    Read a DNS‐encoded name (with compression pointers) from data[offset:].
    Returns (name, new_offset).
    """
    labels = []
    while True:
        length = data[offset]
        # compression pointer?
        if length & 0xC0 == 0xC0:
            pointer = struct.unpack_from("!H", data, offset)[0] & 0x3FFF
            subname, _ = read_name(data, pointer)
            labels.append(subname)
            offset += 2
            break
        if length == 0:
            offset += 1
            break
        offset += 1
        labels.append(data[offset : offset + length].decode())
        offset += length
    return ".".join(labels), offset


def parse_echconfigs(buf: bytes) -> list[str]:
    """
    buf is the raw bytes of the ECHConfig vector:
      - 2-byte total length, then for each:
        - 2-byte cfg length + that many bytes of cfg
    We return a list of Base64 strings (one per config).
    """
    if len(buf) < 2:
        return []
    off = 2
    total = struct.unpack_from("!H", buf, 0)[0]
    end = 2 + total
    out = []
    while off + 2 <= end:
        cfg_len = struct.unpack_from("!H", buf, off)[0]
        off += 2
        cfg = buf[off : off + cfg_len]
        off += cfg_len
        out.append(base64.b64encode(cfg).decode())
    return out


def parse_https_rdata(rdata: bytes) -> HttpsRecord:
    """
    Parse the RDATA of an SVCB/HTTPS record.
    Returns a dict with keys: priority, target, alpn, ipv4hint, ipv6hint, echconfig.
    """
    off = 0
    priority = struct.unpack_from("!H", rdata, off)[0]
    off += 2

    target, off = read_name(rdata, off)

    # pull out all the key/value params
    params = {}
    while off + 4 <= len(rdata):
        key, length = struct.unpack_from("!HH", rdata, off)
        off += 4
        params[key] = rdata[off : off + length]
        off += length

    # decode ALPN (key=1), IPv4 (4), IPv6 (6), ECHConfig (5)
    def parse_alpn(buf: bytes) -> list[str]:
        out = []
        i: int = 0
        while i < len(buf):
            ln = buf[i]
            out.append(buf[i + 1 : i + 1 + ln].decode())
            i += 1 + ln
        return out

    alpn: list[str] = parse_alpn(params.get(1, b""))
    ipv4 = [
        inet4_ntoa(params[4][i : i + 4]) for i in range(0, len(params.get(4, b"")), 4)
    ]
    ipv6 = [
        inet6_ntoa(params[6][i : i + 16]) for i in range(0, len(params.get(6, b"")), 16)
    ]
    echconfs = parse_echconfigs(params.get(5, b""))

    return {
        "priority": priority,
        "target": target or ".",  # empty name → root
        "alpn": alpn,
        "ipv4hint": ipv4,
        "ipv6hint": ipv6,
        "echconfig": echconfs,
    }


__all__ = (
    "inet4_ntoa",
    "inet6_ntoa",
    "packet_fragment",
    "is_ipv4",
    "is_ipv6",
    "validate_length_of",
    "rfc1035_pack",
    "rfc1035_unpack",
    "rfc1035_should_read",
    "parse_https_rdata",
)
