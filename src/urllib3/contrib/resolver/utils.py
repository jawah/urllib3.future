from __future__ import annotations

import binascii
import socket


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


__all__ = (
    "inet4_ntoa",
    "inet6_ntoa",
    "packet_fragment",
    "is_ipv4",
    "is_ipv6",
    "validate_length_of",
)
