from __future__ import annotations

import typing
from collections import deque

from .events import Event


class StreamMatrix:
    """Efficient way to store events for concurrent streams."""

    def __init__(self) -> None:
        self._matrix: dict[int | None, deque[Event]] = {}
        self._count: int = 0
        self._event_cursor_id: int = 0
        self._streams: list[int] | None = []

    def __len__(self) -> int:
        return self._count

    def __iadd__(self, other: typing.Iterable[Event]) -> StreamMatrix:
        self.extend(other)
        return self

    def __bool__(self) -> bool:
        return self.count() > 0

    @property
    def streams(self) -> list[int]:
        if self._streams is not None:
            return self._streams
        available_streams: list[int] = [
            i for i in self._matrix.keys() if isinstance(i, int)
        ]
        self._streams = list(sorted(available_streams))
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

        have_global_event: bool = None in self._matrix and len(self._matrix[None]) > 0

        if stream_id is None and self.streams and self.count(self.streams[0]) > 0:
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

            if stream_id is not None and len(self._matrix[stream_id]) == 0:
                del self._matrix[stream_id]
                self._streams = None

        return ev

    def count(self, stream_id: int | None = None) -> int:
        if stream_id is None:
            return self._count
        if stream_id not in self._matrix:
            return 0

        return len(self._matrix[stream_id])
