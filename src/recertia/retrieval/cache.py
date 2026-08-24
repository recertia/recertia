"""Exact-match retrieval cache keyed by query + index snapshot + env fingerprint."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping

from recertia.ops.systems import canonical_args_hash
from recertia.solver.result_cache import CacheStats


@dataclass
class _Hit:
    bundle: Any
    explanation: Any
    stored_at: float
    snapshot_id: str


class RetrievalCache:
    def __init__(self, *, ttl_s: float = 30.0, enabled: bool = False) -> None:
        self.ttl_s = ttl_s
        self.enabled = enabled
        self.stats = CacheStats()
        self._entries: dict[str, _Hit] = {}

    def _key(self, query: str, snapshot_id: str, env_fingerprint: Mapping[str, Any] | None) -> str:
        env = canonical_args_hash(dict(env_fingerprint or {}))
        return f"{snapshot_id}:{canonical_args_hash({'q': query})}:{env}"

    def lookup(
        self,
        query: str,
        *,
        snapshot_id: str,
        env_fingerprint: Mapping[str, Any] | None = None,
    ) -> tuple[Any, Any] | None:
        if not self.enabled:
            self.stats.skipped += 1
            return None
        key = self._key(query, snapshot_id, env_fingerprint)
        hit = self._entries.get(key)
        if hit is None:
            self.stats.misses += 1
            return None
        if time.monotonic() - hit.stored_at > self.ttl_s:
            self._entries.pop(key, None)
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return hit.bundle, hit.explanation

    def store(
        self,
        query: str,
        bundle: Any,
        explanation: Any,
        *,
        snapshot_id: str,
        env_fingerprint: Mapping[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return
        key = self._key(query, snapshot_id, env_fingerprint)
        self._entries[key] = _Hit(
            bundle=bundle,
            explanation=explanation,
            stored_at=time.monotonic(),
            snapshot_id=snapshot_id,
        )
        self.stats.stores += 1

    def invalidate_snapshot(self, snapshot_id: str) -> int:
        drop = [k for k, e in self._entries.items() if e.snapshot_id == snapshot_id]
        for key in drop:
            del self._entries[key]
        return len(drop)

    def invalidate_all(self) -> None:
        self._entries.clear()
