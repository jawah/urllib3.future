from __future__ import annotations

import typing
from collections import deque

from .events import Event


class StreamMatrix:
    """Efficient way to store events for concurrent streams."""

    __slots__ = (
        "_matrix",
        "_count",
        "_event_cursor_id",
        "_streams",
    )

    def __init__(self) -> None:
        self._matrix: dict[int | None, deque[Event]] = {}
        self._count: int = 0
        self._event_cursor_id: int = 0
        self._streams: list[int] | None = []

    def __len__(self) -> int:
        return self._count

    def __bool__(self) -> bool:
        return self._count > 0

    @property
    def streams(self) -> list[int]:
        if self._streams is not None:
            return self._streams
        self._streams = sorted(i for i in self._matrix.keys() if isinstance(i, int))
        return self._streams

    def append(self, event: Event) -> None:
        matrix_idx = None if not hasattr(event, "stream_id") else event.stream_id

        event._id = self._event_cursor_id
        self._event_cursor_id += 1

        if matrix_idx not in self._matrix:
            self._streams = None
            self._matrix[matrix_idx] = deque()

        self._matrix[matrix_idx].append(event)

        self._count += 1

    def extend(self, events: typing.Iterable[Event]) -> None:
        triaged_events: dict[int | None, list[Event]] = {}

        for event in events:
            matrix_idx = None if not hasattr(event, "stream_id") else event.stream_id

            event._id = self._event_cursor_id

            self._event_cursor_id += 1
            self._count += 1

            if matrix_idx not in triaged_events:
                triaged_events[matrix_idx] = []

            triaged_events[matrix_idx].append(event)

        for k, v in triaged_events.items():
            if k not in self._matrix:
                self._matrix[k] = deque()

            self._matrix[k].extend(v)

        self._streams = None

    def appendleft(self, event: Event) -> None:
        matrix_idx = None if not hasattr(event, "stream_id") else event.stream_id
        event._id = self._event_cursor_id
        self._event_cursor_id += 1

        if matrix_idx not in self._matrix:
            self._streams = None
            self._matrix[matrix_idx] = deque()

        self._matrix[matrix_idx].appendleft(event)

        self._count += 1

    def popleft(self, stream_id: int | None = None) -> Event | None:
        if self._count == 0:
            return None

        have_global_event: bool = None in self._matrix and bool(self._matrix[None])

        if stream_id is None and self.streams and self.has(self.streams[0]):
            stream_id = self.streams[0]

        if (
            stream_id is not None
            and have_global_event
            and stream_id in self._matrix
            and self._matrix[None][0]._id < self._matrix[stream_id][0]._id
        ):
            stream_id = None
        elif have_global_event is True and stream_id not in self._matrix:
            stream_id = None

        if stream_id not in self._matrix:
            return None

        ev = self._matrix[stream_id].popleft()

        if ev is not None:
            self._count -= 1

            if stream_id is not None and not self._matrix[stream_id]:
                del self._matrix[stream_id]
                self._streams = None

        return ev

    def count(
        self,
        stream_id: int | None = None,
        excl_event: tuple[type[Event], ...] | None = None,
    ) -> int:
        if stream_id is None:
            return self._count
        if stream_id not in self._matrix:
            return 0

        return len(
            self._matrix[stream_id]
            if excl_event is None
            else [e for e in self._matrix[stream_id] if not isinstance(e, excl_event)]
        )

    def has(
        self,
        stream_id: int | None = None,
        excl_event: tuple[type[Event], ...] | None = None,
    ) -> bool:
        if stream_id is None:
            return True if self._count else False
        if stream_id not in self._matrix:
            return False

        if excl_event is not None:
            return any(
                e for e in self._matrix[stream_id] if not isinstance(e, excl_event)
            )

        return True if self._matrix[stream_id] else False
