"""Read-only tool-result cache (ADR-0018).

Key is (tool, canonical args, workspace snapshot hash). Write / network / external
tools are never stored. TTL is short; callers invalidate on snapshot change.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from typing import Any, Mapping

from recertia.ops.systems import canonical_tool_key
from recertia.solver.registry import Tool, ToolResult

CACHEABLE_SIDE_EFFECTS = frozenset({"read", "pure"})


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
class _Entry:
    result: ToolResult
    stored_at: float
    snapshot_hash: str


class ToolResultCache:
    """In-process exact-match cache. Not durable; T0 and rebuildable."""

    def __init__(self, *, ttl_s: float = 120.0, enabled: bool = True) -> None:
        self.ttl_s = ttl_s
        self.enabled = enabled
        self.stats = CacheStats()
        self._entries: dict[str, _Entry] = {}

    def eligible(self, tool: Tool) -> bool:
        return tool.side_effect in CACHEABLE_SIDE_EFFECTS

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
        entry = self._entries.get(key)
        if entry is None:
            self.stats.misses += 1
            return None
        if time.monotonic() - entry.stored_at > self.ttl_s:
            self._entries.pop(key, None)
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return copy.copy(entry.result)

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
        self._entries[key] = _Entry(
            result=copy.copy(result),
            stored_at=time.monotonic(),
            snapshot_hash=snapshot_hash,
        )
        self.stats.stores += 1

    def invalidate_snapshot(self, snapshot_hash: str) -> int:
        drop = [k for k, e in self._entries.items() if e.snapshot_hash == snapshot_hash]
        for key in drop:
            del self._entries[key]
        return len(drop)

    def invalidate_all(self) -> None:
        self._entries.clear()
