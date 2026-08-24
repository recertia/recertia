"""Exact-match TTL store for read-only caches (ADR-0018).

Callers decide eligibility. Write / network / external tools must never be stored.
Index rebuilds and mutating tools flush via ``drop_if`` / ``clear``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

CACHEABLE_SIDE_EFFECTS = frozenset({"read", "pure"})


def is_cacheable_side_effect(side_effect: str) -> bool:
    return side_effect in CACHEABLE_SIDE_EFFECTS


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    stores: int = 0
    skipped: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


@dataclass
class ExactMatchTtl:
    """In-process exact-match map with a wall-clock TTL. Not durable."""

    ttl_s: float
    _items: dict[str, tuple[float, Any]] = field(default_factory=dict)

    def get(self, key: str) -> Any | None:
        item = self._items.get(key)
        if item is None:
            return None
        stored_at, value = item
        if time.monotonic() - stored_at > self.ttl_s:
            self._items.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._items[key] = (time.monotonic(), value)

    def drop_if(self, predicate: Callable[[str, Any], bool]) -> int:
        drop = [k for k, (_, v) in self._items.items() if predicate(k, v)]
        for key in drop:
            del self._items[key]
        return len(drop)

    def clear(self) -> None:
        self._items.clear()
