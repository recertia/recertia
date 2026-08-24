"""Exact-match retrieval cache keyed by query + index snapshot + env fingerprint."""

from __future__ import annotations

from typing import Any, Mapping

from recertia.cache import CacheStats, ExactMatchTtl
from recertia.ops.systems import canonical_args_hash


class RetrievalCache:
    def __init__(self, *, ttl_s: float = 30.0, enabled: bool = False) -> None:
        self.ttl_s = ttl_s
        self.enabled = enabled
        self.stats = CacheStats()
        self._store = ExactMatchTtl(ttl_s)

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
        found = self._store.get(key)
        if found is None:
            self.stats.misses += 1
            return None
        bundle, explanation, stored_snap = found
        if stored_snap != snapshot_id:
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return bundle, explanation

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
        self._store.set(key, (bundle, explanation, snapshot_id))
        self.stats.stores += 1

    def invalidate_snapshot(self, snapshot_id: str) -> int:
        return self._store.drop_if(lambda _k, value: value[2] == snapshot_id)

    def invalidate_all(self) -> None:
        self._store.clear()
