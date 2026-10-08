"""Bounded index queries. No source-history materialization or Python sorting."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from itertools import islice
from typing import Any

import dbzero as db0  # type: ignore[import-untyped]

from ..errors import TraderError
from ..serialization import storage_datetime
from .session import Session
from .types import Cursor, Page


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TraderError("INVALID_RANGE", "timestamps must be timezone-aware")
    return storage_datetime(value)


def page[T](
    session: Session,
    model: type[Any],
    convert: Callable[[Any], T],
    *,
    tags: tuple[object, ...] = (),
    start: datetime | None = None,
    end: datetime | None = None,
    time_field: str = "created_at",
    limit: int = 100,
    cursor: Cursor | None = None,
) -> Page[T]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise TraderError("INVALID_PAGE_SIZE", "page size must be between 1 and 1000")
    start = None if start is None else utc(start)
    end = None if end is None else utc(end)
    if start is not None and end is not None and start >= end:
        raise TraderError("INVALID_RANGE", "start must precede end")
    signature = (model, tags, start, end, time_field)
    index = db0.index_of(model, "sequence", prefix=session._prefix)
    if cursor is not None:
        if cursor._scope is not session._scope or cursor._query != signature:
            raise TraderError("INVALID_CURSOR", "cursor belongs to a different session or query")
        after, ceiling = cursor._after, cursor._ceiling
    else:
        latest = next(iter(index.sort(db0.find(model, prefix=session._prefix), desc=True)), None)
        after, ceiling = 0, 0 if latest is None else int(latest.sequence)
    predicates: list[Any] = [model, *tags, index.select(after + 1, ceiling)]
    if start is not None or end is not None:
        time_index = db0.index_of(model, time_field, prefix=session._prefix)
        predicates.append(time_index.select(start, None if end is None else end - timedelta(milliseconds=1)))
    query = db0.find(*predicates, prefix=session._prefix)
    rows = list(islice(index.sort(query), limit + 1))
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = Cursor(session._scope, signature, int(rows[-1].sequence), ceiling) if has_more else None
    return Page(tuple(convert(row) for row in rows), next_cursor)
