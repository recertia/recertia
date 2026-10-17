"""Affordance plane: derived aggregates per tool / resource (specs §13.4, M2).

T0 — rebuildable from the run store / invocation log. ``plan`` and ``classify_failure``
consult flake rate before classifying a failure as ``execution``: a known-flaky tool
produces ``tool``, which does not damage skill trust.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from contracts.resources import ResourceConflict
from recertia.solver.tools import ToolResult


@dataclass
class ToolAffordance:
    tool: str
    invocations: int = 0
    failures: int = 0
    durations_s: list[float] = field(default_factory=list)
    error_signatures: dict[str, int] = field(default_factory=dict)
    costs_usd: list[float] = field(default_factory=list)

    @property
    def failure_rate(self) -> float:
        return self.failures / self.invocations if self.invocations else 0.0

    @property
    def flake_rate(self) -> float:
        # M2: treat failure_rate itself as the flake signal when the tool is marked flaky
        # in the registry; callers combine this with Tool.flaky.
        return self.failure_rate

    @property
    def p50_duration_s(self) -> float | None:
        if not self.durations_s:
            return None
        s = sorted(self.durations_s)
        return s[len(s) // 2]

    @property
    def p95_duration_s(self) -> float | None:
        if not self.durations_s:
            return None
        s = sorted(self.durations_s)
        return s[min(len(s) - 1, int(len(s) * 0.95))]


@dataclass
class ResourceAffordance:
    kind: str
    id: str
    waits_ms: list[int] = field(default_factory=list)
    conflicts: int = 0
    timeouts: int = 0
    observed_ceiling: int = 1

    @property
    def conflict_rate(self) -> float:
        total = len(self.waits_ms) + self.timeouts
        return self.conflicts / total if total else 0.0

    @property
    def p95_wait_ms(self) -> float | None:
        if not self.waits_ms:
            return None
        s = sorted(self.waits_ms)
        return float(s[min(len(s) - 1, int(len(s) * 0.95))])


class AffordanceStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.tools: dict[str, ToolAffordance] = {}
        self.resources: dict[tuple[str, str], ResourceAffordance] = {}
        self._load()

    def _quarantine(self) -> None:
        dest = self.path.with_name(f"{self.path.name}.corrupt-{time.time_ns()}")
        os.replace(self.path, dest)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            self._quarantine()
            return
        if not isinstance(data, dict):
            self._quarantine()
            return
        for name, raw in data.get("tools", {}).items():
            self.tools[name] = ToolAffordance(**raw)
        for key, raw in data.get("resources", {}).items():
            kind, rid = key.split(":", 1)
            self.resources[(kind, rid)] = ResourceAffordance(**raw)

    def save(self) -> None:
        with self._lock:
            payload = {
                "tools": {
                    name: {
                        "tool": a.tool,
                        "invocations": a.invocations,
                        "failures": a.failures,
                        "durations_s": a.durations_s[-200:],
                        "error_signatures": a.error_signatures,
                        "costs_usd": a.costs_usd[-200:],
                    }
                    for name, a in self.tools.items()
                },
                "resources": {
                    f"{k}:{i}": {
                        "kind": a.kind,
                        "id": a.id,
                        "waits_ms": a.waits_ms[-200:],
                        "conflicts": a.conflicts,
                        "timeouts": a.timeouts,
                        "observed_ceiling": a.observed_ceiling,
                    }
                    for (k, i), a in self.resources.items()
                },
            }
            from recertia.fsutil import atomic_write_text, durability_from_settings
            from recertia.settings import load_settings

            atomic_write_text(
                self.path,
                json.dumps(payload, indent=2) + "\n",
                durability=durability_from_settings(load_settings()),
            )

    def record_tool(self, result: ToolResult) -> None:
        with self._lock:
            agg = self.tools.setdefault(result.tool, ToolAffordance(tool=result.tool))
            agg.invocations += 1
            if not result.ok:
                agg.failures += 1
            agg.durations_s.append(result.duration_s)
            agg.costs_usd.append(result.cost_usd)
            if result.error_signature:
                agg.error_signatures[result.error_signature] = (
                    agg.error_signatures.get(result.error_signature, 0) + 1
                )

    def record_conflict(self, conflict: ResourceConflict) -> None:
        with self._lock:
            key = (conflict.claim.kind, conflict.claim.id)
            agg = self.resources.setdefault(
                key, ResourceAffordance(kind=conflict.claim.kind, id=conflict.claim.id)
            )
            agg.conflicts += 1
            agg.waits_ms.append(conflict.waited_ms)
            if conflict.resolution == "timed_out":
                agg.timeouts += 1
            # Ceiling of 1 when we ever had to wait — contention observed.
            if conflict.waited_ms > 0:
                agg.observed_ceiling = 1

    def tool(self, name: str) -> ToolAffordance | None:
        return self.tools.get(name)

    def is_known_flaky(self, tool_name: str, *, threshold: float = 0.3) -> bool:
        agg = self.tools.get(tool_name)
        return bool(agg and agg.invocations >= 3 and agg.flake_rate >= threshold)

    def matches_error_signature(self, tool_name: str, output: str) -> str | None:
        agg = self.tools.get(tool_name)
        if not agg:
            return None
        for sig in agg.error_signatures:
            if sig in output:
                return sig
        return None
