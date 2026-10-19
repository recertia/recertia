"""Affordance plane: derived aggregates per tool / resource (specs §13.4, M2).

T0 — rebuildable from the run store / invocation log. ``plan`` and ``classify_failure``
consult flake rate before classifying a failure as ``execution``: a known-flaky tool
produces ``tool``, which does not damage skill trust.

Counters live in ``runtime.db`` (REL-5 / F19). Concurrent attempts increment under
``BEGIN IMMEDIATE`` rather than rewriting ``affordances.json``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from contracts.resources import ResourceConflict
from recertia.persistence.db import open_db
from recertia.solver.tools import ToolResult

_DEFAULT_CAP = 200


def _sample_cap() -> int:
    try:
        from recertia.settings import load_settings

        return max(1, int(load_settings().affordance_sample_cap))
    except Exception:
        return _DEFAULT_CAP


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


def _tool_from_row(row: sqlite3.Row) -> ToolAffordance:
    durations = json.loads(row["durations_json"] or "[]")
    costs = json.loads(row["costs_json"] or "[]")
    signatures = json.loads(row["error_signatures_json"] or "{}")
    return ToolAffordance(
        tool=str(row["tool"]),
        invocations=int(row["invocations"]),
        failures=int(row["failures"]),
        durations_s=[float(x) for x in durations],
        error_signatures={str(k): int(v) for k, v in dict(signatures).items()},
        costs_usd=[float(x) for x in costs],
    )


def _resource_from_row(row: sqlite3.Row) -> ResourceAffordance:
    waits = json.loads(row["waits_json"] or "[]")
    return ResourceAffordance(
        kind=str(row["kind"]),
        id=str(row["resource_id"]),
        waits_ms=[int(x) for x in waits],
        conflicts=int(row["conflicts"]),
        timeouts=int(row["timeouts"]),
        observed_ceiling=int(row["observed_ceiling"]),
    )


class AffordanceStore:
    def __init__(self, path: Path | str, *, tenant_id: str = "local") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._tenant_id = tenant_id
        self._db_path = self.path.parent / "runtime.db"
        self._lock = threading.Lock()
        self._conn = open_db(self._db_path, timeout=30.0)
        self._conn.isolation_level = None
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()
        self.tools: dict[str, ToolAffordance] = {}
        self.resources: dict[tuple[str, str], ResourceAffordance] = {}
        self._import_json_once()
        self._reload()

    def close(self) -> None:
        self._conn.close()

    def _ensure_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS affordance_tools (
                tenant_id              TEXT NOT NULL,
                tool                   TEXT NOT NULL,
                invocations            INTEGER NOT NULL DEFAULT 0,
                failures               INTEGER NOT NULL DEFAULT 0,
                durations_json         TEXT NOT NULL DEFAULT '[]',
                error_signatures_json  TEXT NOT NULL DEFAULT '{}',
                costs_json             TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (tenant_id, tool)
            );
            CREATE TABLE IF NOT EXISTS affordance_resources (
                tenant_id         TEXT NOT NULL,
                kind              TEXT NOT NULL,
                resource_id       TEXT NOT NULL,
                waits_json        TEXT NOT NULL DEFAULT '[]',
                conflicts         INTEGER NOT NULL DEFAULT 0,
                timeouts          INTEGER NOT NULL DEFAULT 0,
                observed_ceiling  INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (tenant_id, kind, resource_id)
            );
            """
        )

    def _quarantine(self) -> None:
        dest = self.path.with_name(f"{self.path.name}.corrupt-{time.time_ns()}")
        os.replace(self.path, dest)

    def _import_json_once(self) -> None:
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
        cap = _sample_cap()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for name, raw in dict(data.get("tools") or {}).items():
                if not isinstance(raw, dict):
                    continue
                agg = ToolAffordance(
                    tool=str(raw.get("tool") or name),
                    invocations=int(raw.get("invocations") or 0),
                    failures=int(raw.get("failures") or 0),
                    durations_s=[float(x) for x in list(raw.get("durations_s") or [])][-cap:],
                    error_signatures={
                        str(k): int(v) for k, v in dict(raw.get("error_signatures") or {}).items()
                    },
                    costs_usd=[float(x) for x in list(raw.get("costs_usd") or [])][-cap:],
                )
                self._conn.execute(
                    """
                    INSERT INTO affordance_tools (
                        tenant_id, tool, invocations, failures,
                        durations_json, error_signatures_json, costs_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (tenant_id, tool) DO NOTHING
                    """,
                    (
                        self._tenant_id,
                        agg.tool,
                        agg.invocations,
                        agg.failures,
                        json.dumps(agg.durations_s),
                        json.dumps(agg.error_signatures),
                        json.dumps(agg.costs_usd),
                    ),
                )
            for key, raw in dict(data.get("resources") or {}).items():
                if not isinstance(raw, dict):
                    continue
                kind = str(raw.get("kind") or "")
                rid = str(raw.get("id") or "")
                if not kind or not rid:
                    parts = str(key).split(":", 1)
                    if len(parts) == 2:
                        kind, rid = parts
                if not kind or not rid:
                    continue
                waits = [int(x) for x in list(raw.get("waits_ms") or [])][-cap:]
                self._conn.execute(
                    """
                    INSERT INTO affordance_resources (
                        tenant_id, kind, resource_id, waits_json,
                        conflicts, timeouts, observed_ceiling
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (tenant_id, kind, resource_id) DO NOTHING
                    """,
                    (
                        self._tenant_id,
                        kind,
                        rid,
                        json.dumps(waits),
                        int(raw.get("conflicts") or 0),
                        int(raw.get("timeouts") or 0),
                        int(raw.get("observed_ceiling") or 1),
                    ),
                )
            self._conn.execute("COMMIT")
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        dest = self.path.with_name(f"{self.path.name}.imported")
        if dest.exists():
            dest = self.path.with_name(f"{self.path.name}.imported-{time.time_ns()}")
        os.replace(self.path, dest)

    def _reload(self) -> None:
        tools: dict[str, ToolAffordance] = {}
        for row in self._conn.execute(
            "SELECT * FROM affordance_tools WHERE tenant_id = ?",
            (self._tenant_id,),
        ):
            tool_agg = _tool_from_row(row)
            tools[tool_agg.tool] = tool_agg
        resources: dict[tuple[str, str], ResourceAffordance] = {}
        for row in self._conn.execute(
            "SELECT * FROM affordance_resources WHERE tenant_id = ?",
            (self._tenant_id,),
        ):
            resource_agg = _resource_from_row(row)
            resources[(resource_agg.kind, resource_agg.id)] = resource_agg
        self.tools = tools
        self.resources = resources

    def save(self) -> None:
        """No-op. Increments commit inside ``record_tool`` / ``record_conflict``."""

    def record_tool(self, result: ToolResult) -> None:
        cap = _sample_cap()
        failure = 0 if result.ok else 1
        signature = result.error_signature
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    """
                    SELECT invocations, failures, durations_json, error_signatures_json, costs_json
                    FROM affordance_tools WHERE tenant_id = ? AND tool = ?
                    """,
                    (self._tenant_id, result.tool),
                ).fetchone()
                if row is None:
                    durations = [result.duration_s][-cap:]
                    costs = [result.cost_usd][-cap:]
                    signatures: dict[str, int] = {signature: 1} if signature else {}
                    self._conn.execute(
                        """
                        INSERT INTO affordance_tools (
                            tenant_id, tool, invocations, failures,
                            durations_json, error_signatures_json, costs_json
                        ) VALUES (?, ?, 1, ?, ?, ?, ?)
                        """,
                        (
                            self._tenant_id,
                            result.tool,
                            failure,
                            json.dumps(durations),
                            json.dumps(signatures),
                            json.dumps(costs),
                        ),
                    )
                else:
                    durations = [float(x) for x in json.loads(row["durations_json"] or "[]")]
                    durations.append(result.duration_s)
                    durations = durations[-cap:]
                    costs = [float(x) for x in json.loads(row["costs_json"] or "[]")]
                    costs.append(result.cost_usd)
                    costs = costs[-cap:]
                    signatures = {
                        str(k): int(v)
                        for k, v in dict(json.loads(row["error_signatures_json"] or "{}")).items()
                    }
                    if signature:
                        signatures[signature] = signatures.get(signature, 0) + 1
                    self._conn.execute(
                        """
                        UPDATE affordance_tools SET
                            invocations = invocations + 1,
                            failures = failures + ?,
                            durations_json = ?,
                            error_signatures_json = ?,
                            costs_json = ?
                        WHERE tenant_id = ? AND tool = ?
                        """,
                        (
                            failure,
                            json.dumps(durations),
                            json.dumps(signatures),
                            json.dumps(costs),
                            self._tenant_id,
                            result.tool,
                        ),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise
            self._reload()

    def record_conflict(self, conflict: ResourceConflict) -> None:
        cap = _sample_cap()
        kind = conflict.claim.kind
        rid = conflict.claim.id
        timed_out = 1 if conflict.resolution == "timed_out" else 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    """
                    SELECT waits_json, conflicts, timeouts, observed_ceiling
                    FROM affordance_resources
                    WHERE tenant_id = ? AND kind = ? AND resource_id = ?
                    """,
                    (self._tenant_id, kind, rid),
                ).fetchone()
                if row is None:
                    waits = [conflict.waited_ms][-cap:]
                    ceiling = 1 if conflict.waited_ms > 0 else 1
                    self._conn.execute(
                        """
                        INSERT INTO affordance_resources (
                            tenant_id, kind, resource_id, waits_json,
                            conflicts, timeouts, observed_ceiling
                        ) VALUES (?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            self._tenant_id,
                            kind,
                            rid,
                            json.dumps(waits),
                            timed_out,
                            ceiling,
                        ),
                    )
                else:
                    waits = [int(x) for x in json.loads(row["waits_json"] or "[]")]
                    waits.append(conflict.waited_ms)
                    waits = waits[-cap:]
                    ceiling = int(row["observed_ceiling"])
                    if conflict.waited_ms > 0:
                        ceiling = 1
                    self._conn.execute(
                        """
                        UPDATE affordance_resources SET
                            waits_json = ?,
                            conflicts = conflicts + 1,
                            timeouts = timeouts + ?,
                            observed_ceiling = ?
                        WHERE tenant_id = ? AND kind = ? AND resource_id = ?
                        """,
                        (
                            json.dumps(waits),
                            timed_out,
                            ceiling,
                            self._tenant_id,
                            kind,
                            rid,
                        ),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise
            self._reload()

    def tool(self, name: str) -> ToolAffordance | None:
        row = self._conn.execute(
            "SELECT * FROM affordance_tools WHERE tenant_id = ? AND tool = ?",
            (self._tenant_id, name),
        ).fetchone()
        if row is None:
            return None
        return _tool_from_row(row)

    def is_known_flaky(self, tool_name: str, *, threshold: float = 0.3) -> bool:
        agg = self.tool(tool_name)
        return bool(agg and agg.invocations >= 3 and agg.flake_rate >= threshold)

    def matches_error_signature(self, tool_name: str, output: str) -> str | None:
        agg = self.tool(tool_name)
        if not agg:
            return None
        for sig in agg.error_signatures:
            if sig in output:
                return sig
        return None
