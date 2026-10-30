"""Affordance plane: derived aggregates per tool / resource (specs §13.4, M2).

T0 — rebuildable from the run store / invocation log. ``plan`` and ``classify_failure``
consult flake rate before classifying a failure as ``execution``: a known-flaky tool
produces ``tool``, which does not damage skill trust.

Counters live in the runs root's ``runtime.db`` (tables from runtime migration
``004_affordance``), keyed by tenant. Every record is one ``BEGIN IMMEDIATE``
transaction of SQL increments, so processes sharing a runs root do not lose each
other's counts. A legacy ``affordances.json`` beside it is imported once, by the
first write; reads never create, migrate or import.
"""

from __future__ import annotations

import itertools
import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contracts.resources import ResourceConflict
from recertia.errors import ConfigError, InvalidTenantId
from recertia.persistence.db import apply_migrations, open_db
from recertia.ports.identity import tenant_id
from recertia.settings import affordance_sample_cap, load_settings
from recertia.solver.tools import ToolResult

# ``schema_migrations`` row written in the transaction that imports (or finds no)
# legacy ``affordances.json``; its ``checksum`` column says which.
IMPORT_MARKER = "runtime.import_affordances_json"
_RUNTIME_MIGRATIONS = Path(__file__).resolve().parents[2] / "persistence" / "migrations" / "runtime"


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


def locate_runtime_db(path: Path | str) -> tuple[Path, str]:
    """``runtime.db`` and the tenant for an ``affordances.json`` path.

    The file sits in its runs root, beside ``runtime.db``. Under ``<root>/runs/<tenant>``
    the tenant is that directory's name, which must be a valid tenant id; anywhere else
    (the CLI's runs root is the process root) it is ``local``.
    """

    parent = Path(path).parent
    if parent.parent.name == "runs":
        try:
            tenant = str(tenant_id(parent.name))
        except InvalidTenantId as exc:
            raise ConfigError(
                "affordances path is under runs/ but its directory is not a valid tenant id",
                details={"directory": parent.name},
            ) from exc
        return parent / "runtime.db", tenant
    return parent / "runtime.db", "local"


def _reserve_aside(path: Path, names: Iterator[str]) -> Path:
    """Create the first free name beside ``path`` exclusively; never reuse an existing file."""

    for name in names:
        dest = path.with_name(name)
        try:
            fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        os.close(fd)
        return dest
    raise AssertionError("unreachable: the name iterator is endless")


def _move_aside(path: Path, names: Iterator[str]) -> Path | None:
    """Rename ``path`` to a reserved fresh name. ``None`` when ``path`` is already gone."""

    dest = _reserve_aside(path, names)
    try:
        os.replace(path, dest)
    except FileNotFoundError:
        dest.unlink(missing_ok=True)
        return None
    return dest


def _corrupt_names(path: Path) -> Iterator[str]:
    stem = f"{path.name}.corrupt-{time.time_ns()}"
    yield stem
    for n in itertools.count(1):
        yield f"{stem}-{n}"


def _imported_names(path: Path) -> Iterator[str]:
    yield f"{path.name}.imported"
    stem = f"{path.name}.imported-{time.time_ns()}"
    yield stem
    for n in itertools.count(1):
        yield f"{stem}-{n}"


_Aggregates = tuple[dict[str, "ToolAffordance"], dict[tuple[str, str], "ResourceAffordance"]]


def _parse_legacy(path: Path) -> _Aggregates | None:
    """The legacy file's aggregates, or ``None`` when it is torn or the wrong shape."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        tools = {name: ToolAffordance(**raw) for name, raw in data.get("tools", {}).items()}
        resources: dict[tuple[str, str], ResourceAffordance] = {}
        for key, raw in data.get("resources", {}).items():
            kind, rid = key.split(":", 1)
            resources[(kind, rid)] = ResourceAffordance(**raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError, AttributeError):
        return None
    return tools, resources


class AffordanceStore:
    """Affordance counters for one runs root, in its ``runtime.db``.

    ``RECERTIA_AFFORDANCE_SAMPLE_CAP`` (default 200) is read and checked once, here.
    Per tool the newest ``cap`` durations and costs are kept, per resource the newest
    ``cap`` waits, and per tool at most ``cap`` distinct error signatures, evicting the
    least recently seen.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.runtime_db, self.tenant = locate_runtime_db(self.path)
        self.sample_cap = affordance_sample_cap()
        self._durability = load_settings().durability
        self._lock = threading.Lock()
        self._writer: sqlite3.Connection | None = None

    def close(self) -> None:
        with self._lock:
            if self._writer is not None:
                self._writer.close()
            self._writer = None

    # -- write path -------------------------------------------------------------

    def _write_conn(self) -> sqlite3.Connection:
        """Open, migrate and import once. Only write paths call this."""

        if self._writer is None:
            conn = open_db(self.runtime_db, durability=self._durability)
            try:
                conn.isolation_level = None
                apply_migrations(conn, _RUNTIME_MIGRATIONS)
                self._import_legacy(conn)
            except BaseException:
                conn.close()
                raise
            self._writer = conn
        return self._writer

    @contextmanager
    def _txn(self, conn: sqlite3.Connection) -> Iterator[None]:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def _import_legacy(self, conn: sqlite3.Connection) -> None:
        """Import ``affordances.json`` exactly once per ``runtime.db``.

        The marker row and the imported rows commit together under ``BEGIN IMMEDIATE``;
        a second process waits for the lock, sees the marker and imports nothing. A torn
        file is moved aside as ``.corrupt-<ns>``. The imported file is renamed to
        ``.imported`` after the commit; a crash before the rename leaves it for the next
        writer to rename, never to import again.
        """

        imported = False
        with self._txn(conn):
            marked = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?", (IMPORT_MARKER,)
            ).fetchone()
            if marked is None:
                status = "none"
                if self.path.is_file():
                    parsed = _parse_legacy(self.path)
                    if parsed is None:
                        _move_aside(self.path, _corrupt_names(self.path))
                        status = "corrupt"
                    else:
                        self._insert_legacy(conn, *parsed)
                        status = "imported"
                        imported = True
                conn.execute(
                    "INSERT INTO schema_migrations(version, checksum, applied_at) VALUES (?, ?, ?)",
                    (IMPORT_MARKER, status, datetime.now(timezone.utc).isoformat()),
                )
            else:
                imported = self.path.is_file()
        if imported:
            _move_aside(self.path, _imported_names(self.path))

    def _insert_legacy(
        self,
        conn: sqlite3.Connection,
        tools: dict[str, ToolAffordance],
        resources: dict[tuple[str, str], ResourceAffordance],
    ) -> None:
        cap = self.sample_cap
        for name, agg in tools.items():
            conn.execute(
                "INSERT INTO affordance_tools(tenant_id, tool, invocations, failures, seq)"
                " VALUES (?, ?, ?, ?, 0)",
                (self.tenant, name, agg.invocations, agg.failures),
            )
            pairs = list(zip(agg.durations_s, agg.costs_usd, strict=False))[-cap:]
            conn.executemany(
                "INSERT INTO affordance_tool_samples(tenant_id, tool, duration_s, cost_usd)"
                " VALUES (?, ?, ?, ?)",
                [(self.tenant, name, d, c) for d, c in pairs],
            )
            top = sorted(agg.error_signatures.items(), key=lambda kv: -kv[1])[:cap]
            conn.executemany(
                "INSERT INTO affordance_error_signatures(tenant_id, tool, signature, count, last_seq)"
                " VALUES (?, ?, ?, ?, 0)",
                [(self.tenant, name, sig, count) for sig, count in top],
            )
        for (kind, rid), res in resources.items():
            conn.execute(
                "INSERT INTO affordance_resources(tenant_id, kind, rid, conflicts, timeouts,"
                " observed_ceiling) VALUES (?, ?, ?, ?, ?, ?)",
                (self.tenant, kind, rid, res.conflicts, res.timeouts, res.observed_ceiling),
            )
            conn.executemany(
                "INSERT INTO affordance_resource_waits(tenant_id, kind, rid, wait_ms) VALUES (?, ?, ?, ?)",
                [(self.tenant, kind, rid, w) for w in res.waits_ms[-cap:]],
            )

    def save(self) -> None:
        """Every record is committed when it returns; this imports a legacy file if none was yet."""

        with self._lock:
            self._write_conn()

    def record_tool(self, result: ToolResult) -> None:
        with self._lock:
            conn = self._write_conn()
            with self._txn(conn):
                conn.execute(
                    "INSERT INTO affordance_tools(tenant_id, tool, invocations, failures, seq)"
                    " VALUES (?, ?, 1, ?, 1)"
                    " ON CONFLICT(tenant_id, tool) DO UPDATE SET"
                    " invocations = invocations + 1,"
                    " failures = failures + excluded.failures,"
                    " seq = seq + 1",
                    (self.tenant, result.tool, 0 if result.ok else 1),
                )
                (seq,) = conn.execute(
                    "SELECT seq FROM affordance_tools WHERE tenant_id=? AND tool=?",
                    (self.tenant, result.tool),
                ).fetchone()
                conn.execute(
                    "INSERT INTO affordance_tool_samples(tenant_id, tool, duration_s, cost_usd)"
                    " VALUES (?, ?, ?, ?)",
                    (self.tenant, result.tool, result.duration_s, result.cost_usd),
                )
                conn.execute(
                    "DELETE FROM affordance_tool_samples WHERE tenant_id=? AND tool=? AND id NOT IN"
                    " (SELECT id FROM affordance_tool_samples WHERE tenant_id=? AND tool=?"
                    " ORDER BY id DESC LIMIT ?)",
                    (self.tenant, result.tool, self.tenant, result.tool, self.sample_cap),
                )
                if result.error_signature:
                    conn.execute(
                        "INSERT INTO affordance_error_signatures(tenant_id, tool, signature, count, last_seq)"
                        " VALUES (?, ?, ?, 1, ?)"
                        " ON CONFLICT(tenant_id, tool, signature) DO UPDATE SET"
                        " count = count + 1, last_seq = excluded.last_seq",
                        (self.tenant, result.tool, result.error_signature, seq),
                    )
                    conn.execute(
                        "DELETE FROM affordance_error_signatures WHERE tenant_id=? AND tool=?"
                        " AND signature NOT IN (SELECT signature FROM affordance_error_signatures"
                        " WHERE tenant_id=? AND tool=? ORDER BY last_seq DESC, count DESC, signature"
                        " LIMIT ?)",
                        (self.tenant, result.tool, self.tenant, result.tool, self.sample_cap),
                    )

    def record_conflict(self, conflict: ResourceConflict) -> None:
        kind, rid = conflict.claim.kind, conflict.claim.id
        timed_out = 1 if conflict.resolution == "timed_out" else 0
        with self._lock:
            conn = self._write_conn()
            with self._txn(conn):
                # Ceiling of 1 when we ever had to wait — contention observed.
                conn.execute(
                    "INSERT INTO affordance_resources(tenant_id, kind, rid, conflicts, timeouts,"
                    " observed_ceiling) VALUES (?, ?, ?, 1, ?, 1)"
                    " ON CONFLICT(tenant_id, kind, rid) DO UPDATE SET"
                    " conflicts = conflicts + 1,"
                    " timeouts = timeouts + excluded.timeouts,"
                    " observed_ceiling = CASE WHEN ? > 0 THEN 1 ELSE observed_ceiling END",
                    (self.tenant, kind, rid, timed_out, conflict.waited_ms),
                )
                conn.execute(
                    "INSERT INTO affordance_resource_waits(tenant_id, kind, rid, wait_ms)"
                    " VALUES (?, ?, ?, ?)",
                    (self.tenant, kind, rid, conflict.waited_ms),
                )
                conn.execute(
                    "DELETE FROM affordance_resource_waits WHERE tenant_id=? AND kind=? AND rid=?"
                    " AND id NOT IN (SELECT id FROM affordance_resource_waits"
                    " WHERE tenant_id=? AND kind=? AND rid=? ORDER BY id DESC LIMIT ?)",
                    (self.tenant, kind, rid, self.tenant, kind, rid, self.sample_cap),
                )

    # -- read path ----------------------------------------------------------------

    def _open_reader(self) -> sqlite3.Connection | None:
        """A read-only handle on an imported ``runtime.db``, or ``None``. Never creates or migrates."""

        if not self.runtime_db.is_file():
            return None
        uri = self.runtime_db.resolve().as_posix().replace("?", "%3F")
        try:
            conn = sqlite3.connect(f"file:{uri}?mode=ro", uri=True, timeout=5.0)
        except sqlite3.Error:
            return None
        conn.isolation_level = None
        try:
            marked = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?", (IMPORT_MARKER,)
            ).fetchone()
        except sqlite3.Error:
            marked = None
        if marked is None:
            conn.close()
            return None
        return conn

    def _legacy_snapshot(self) -> _Aggregates:
        if not self.path.is_file():
            return {}, {}
        try:
            parsed = _parse_legacy(self.path)
        except OSError:
            return {}, {}
        return parsed if parsed is not None else ({}, {})

    def _load_tools(self, conn: sqlite3.Connection, name: str | None) -> dict[str, ToolAffordance]:
        where, args = "tenant_id=?", [self.tenant]
        if name is not None:
            where, args = "tenant_id=? AND tool=?", [self.tenant, name]
        tools: dict[str, ToolAffordance] = {}
        for tool, invocations, failures in conn.execute(
            f"SELECT tool, invocations, failures FROM affordance_tools WHERE {where} ORDER BY rowid",
            args,
        ):
            tools[tool] = ToolAffordance(tool=tool, invocations=invocations, failures=failures)
        for tool, duration_s, cost_usd in conn.execute(
            f"SELECT tool, duration_s, cost_usd FROM affordance_tool_samples WHERE {where} ORDER BY id",
            args,
        ):
            if tool in tools:
                tools[tool].durations_s.append(duration_s)
                tools[tool].costs_usd.append(cost_usd)
        for tool, signature, count in conn.execute(
            f"SELECT tool, signature, count FROM affordance_error_signatures WHERE {where} ORDER BY rowid",
            args,
        ):
            if tool in tools:
                tools[tool].error_signatures[signature] = count
        return tools

    def _load_resources(self, conn: sqlite3.Connection) -> dict[tuple[str, str], ResourceAffordance]:
        resources: dict[tuple[str, str], ResourceAffordance] = {}
        for kind, rid, conflicts, timeouts, ceiling in conn.execute(
            "SELECT kind, rid, conflicts, timeouts, observed_ceiling FROM affordance_resources"
            " WHERE tenant_id=? ORDER BY rowid",
            (self.tenant,),
        ):
            resources[(kind, rid)] = ResourceAffordance(
                kind=kind, id=rid, conflicts=conflicts, timeouts=timeouts, observed_ceiling=ceiling
            )
        for kind, rid, wait_ms in conn.execute(
            "SELECT kind, rid, wait_ms FROM affordance_resource_waits WHERE tenant_id=? ORDER BY id",
            (self.tenant,),
        ):
            if (kind, rid) in resources:
                resources[(kind, rid)].waits_ms.append(wait_ms)
        return resources

    def _read(self, fn: Any) -> Any:
        """Run ``fn`` on the writer, a short-lived read-only handle, or ``None`` (legacy JSON)."""

        with self._lock:
            if self._writer is not None:
                return fn(self._writer)
            conn = self._open_reader()
            if conn is None:
                return fn(None)
            try:
                conn.execute("BEGIN")
                return fn(conn)
            finally:
                conn.close()

    @property
    def tools(self) -> dict[str, ToolAffordance]:
        """Snapshot of every tool's aggregate for this tenant."""

        def load(conn: sqlite3.Connection | None) -> dict[str, ToolAffordance]:
            return self._legacy_snapshot()[0] if conn is None else self._load_tools(conn, None)

        result: dict[str, ToolAffordance] = self._read(load)
        return result

    @property
    def resources(self) -> dict[tuple[str, str], ResourceAffordance]:
        """Snapshot of every resource's aggregate for this tenant."""

        def load(conn: sqlite3.Connection | None) -> dict[tuple[str, str], ResourceAffordance]:
            return self._legacy_snapshot()[1] if conn is None else self._load_resources(conn)

        result: dict[tuple[str, str], ResourceAffordance] = self._read(load)
        return result

    def tool(self, name: str) -> ToolAffordance | None:
        def load(conn: sqlite3.Connection | None) -> ToolAffordance | None:
            tools = self._legacy_snapshot()[0] if conn is None else self._load_tools(conn, name)
            return tools.get(name)

        result: ToolAffordance | None = self._read(load)
        return result

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
