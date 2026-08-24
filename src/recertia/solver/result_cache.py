"""Read-only tool-result cache (ADR-0018).

Key is (tool, canonical args, workspace snapshot hash). Write / network / external
tools are never stored. TTL is short; callers invalidate on snapshot change.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping

from recertia.cache import CACHEABLE_SIDE_EFFECTS, CacheStats, ExactMatchTtl, is_cacheable_side_effect
from recertia.ops.systems import canonical_tool_key
from recertia.solver.registry import Tool, ToolResult

# Re-export so existing imports keep working.
__all__ = [
    "CACHEABLE_SIDE_EFFECTS",
    "CacheStats",
    "ToolResultCache",
    "is_cacheable_side_effect",
]


class ToolResultCache:
    """In-process exact-match cache. Not durable; T0 and rebuildable."""

    def __init__(self, *, ttl_s: float = 120.0, enabled: bool = True) -> None:
        self.ttl_s = ttl_s
        self.enabled = enabled
        self.stats = CacheStats()
        self._store = ExactMatchTtl(ttl_s)

    def eligible(self, tool: Tool) -> bool:
        return is_cacheable_side_effect(tool.side_effect)

    def lookup(
        self,
        tool: Tool,
        inputs: Mapping[str, Any],
        *,
        snapshot_hash: str,
    ) -> ToolResult | None:
        if not self.enabled or not self.eligible(tool):
            self.stats.skipped += 1
            return None
        key = canonical_tool_key(tool.name, inputs, snapshot_hash)
        found = self._store.get(key)
        if found is None:
            self.stats.misses += 1
            return None
        result, stored_snap = found
        if stored_snap != snapshot_hash:
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return copy.copy(result)

    def store(
        self,
        tool: Tool,
        inputs: Mapping[str, Any],
        result: ToolResult,
        *,
        snapshot_hash: str,
    ) -> None:
        if not self.enabled or not self.eligible(tool) or not result.ok:
            self.stats.skipped += 1
            return
        key = canonical_tool_key(tool.name, inputs, snapshot_hash)
        self._store.set(key, (copy.copy(result), snapshot_hash))
        self.stats.stores += 1

    def invalidate_snapshot(self, snapshot_hash: str) -> int:
        return self._store.drop_if(lambda _k, value: value[1] == snapshot_hash)

    def invalidate_all(self) -> None:
        self._store.clear()
