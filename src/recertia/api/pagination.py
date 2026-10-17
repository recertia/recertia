"""In-memory keyset pages. ``cursor`` is exclusive on the sort key."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def keyset_page(
    items: list[T],
    *,
    key: Callable[[T], str],
    limit: int,
    cursor: str | None,
) -> tuple[list[T], str | None]:
    ordered = sorted(items, key=key)
    if cursor:
        ordered = [item for item in ordered if key(item) > cursor]
    page = ordered[:limit]
    next_cursor = key(page[-1]) if page and len(ordered) > limit else None
    return page, next_cursor
