from __future__ import annotations

import re
import typing


def is_fp_closed(obj: object) -> bool:
    """
    Checks whether a given file-like object is closed.

    :param obj:
        The file-like object to check.
    """

    try:
        # Check `isclosed()` first, in case Python3 doesn't set `closed`.
        # GH Issue #928
        return obj.isclosed()  # type: ignore[no-any-return, attr-defined]
    except AttributeError:
        pass

    try:
        # Check via the official file-like-object way.
        return obj.closed  # type: ignore[no-any-return, attr-defined]
    except AttributeError:
        pass

    try:
        # Check if the object is a container for another file-like object that
        # gets released on exhaustion (e.g. HTTPResponse).
        return obj.fp is None  # type: ignore[attr-defined]
    except AttributeError:
        pass

    raise ValueError("Unable to determine whether fp is closed.")


def parse_alt_svc(value: str) -> typing.Iterable[tuple[str, str]]:
    """Given an Alt-Svc value, extract from it the protocol-id and the alt-authority.
    https://httpwg.org/specs/rfc7838.html#alt-svc"""
    pattern = re.compile(
        r"(h[0-9]{1,3}(?:[\-0-9]{0,5}))=(?:[\"\']?)([a-z0-9\-_:.]+)(?:[\"\']?)",
        re.IGNORECASE,
    )

    yield from re.findall(pattern, value)
