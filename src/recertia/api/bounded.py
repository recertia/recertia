"""In-process caches that drop the oldest entry past a cap."""

from __future__ import annotations

from typing import TypeVar

K = TypeVar("K")
V = TypeVar("V")

RUN_CACHE_MAX = 512


class BoundedDict(dict[K, V]):
    """Insertion-ordered dict. A new key past ``max_size`` drops the oldest."""

    def __init__(self, max_size: int = RUN_CACHE_MAX) -> None:
        super().__init__()
        self.max_size = max(1, max_size)

    def __setitem__(self, key: K, value: V) -> None:
        if key in self:
            del self[key]
        super().__setitem__(key, value)
        while len(self) > self.max_size:
            oldest = next(iter(self))
            super().__delitem__(oldest)
