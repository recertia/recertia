"""REC-056d: the affordance store keeps its counters in ``runtime.db``.

Reads never create, migrate or import. The first write imports a legacy
``affordances.json`` once. Every record is one transaction, and the sample and
signature caps are applied inside it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from contracts.resources import ResourceClaim, ResourceConflict
from recertia.errors import ConfigError
from recertia.memory import affordance as affordance_mod
from recertia.memory.affordance import IMPORT_MARKER, AffordanceStore, locate_runtime_db
from recertia.persistence.db import apply_migrations, open_db
from recertia.solver.tools import ToolResult
from tests.support.affordance import (
    check_corrupt_quarantine_never_overwrites,
    check_imported_rename_never_overwrites,
)

_RUNTIME = Path(affordance_mod.__file__).resolve().parents[2] / "persistence" / "migrations" / "runtime"


@pytest.fixture
def store_at() -> Iterator[Callable[[Path], AffordanceStore]]:
    opened: list[AffordanceStore] = []

    def make(path: Path) -> AffordanceStore:
        store = AffordanceStore(path)
        opened.append(store)
        return store

    yield make
    for store in opened:
        store.close()


def _legacy(path: Path, **tools: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "tools": {
            name: {
                "tool": name,
                "invocations": 0,
                "failures": 0,
                "durations_s": [],
                "costs_usd": [],
                "error_signatures": {},
                **raw,
            }
            for name, raw in tools.items()
        },
        "resources": {},
    }
    path.write_text(json.dumps(body), encoding="utf-8")


def _query(db: Path, sql: str, *args: object) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _conflict(rid: str, *, waited_ms: int, timed_out: bool = False) -> ResourceConflict:
    return ResourceConflict(
        claim=ResourceClaim(kind="service", id=rid, mode="exclusive"),
        waiting="b",
        holder="a",
        waited_ms=waited_ms,
        resolution="timed_out" if timed_out else "acquired",
    )


# -- reads do not create, migrate or import ------------------------------------------


def test_reads_on_a_fresh_root_create_nothing(tmp_path: Path, store_at) -> None:
    store = store_at(tmp_path / "affordances.json")
    assert store.tools == {}
    assert store.resources == {}
    assert store.tool("t") is None
    assert store.is_known_flaky("t") is False
    assert store.matches_error_signature("t", "boom") is None
    assert list(tmp_path.iterdir()) == []


def test_reads_use_legacy_json_without_importing_it(tmp_path: Path, store_at) -> None:
    path = tmp_path / "affordances.json"
    _legacy(path, t={"invocations": 4, "failures": 2, "error_signatures": {"boom": 2}})
    store = store_at(path)
    agg = store.tool("t")
    assert agg is not None and (agg.invocations, agg.failures) == (4, 2)
    assert store.is_known_flaky("t") is True
    assert store.matches_error_signature("t", "a boom here") == "boom"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["affordances.json"]


def test_reads_ignore_a_runtime_db_without_the_import_marker(tmp_path: Path, store_at) -> None:
    path = tmp_path / "affordances.json"
    _legacy(path, t={"invocations": 7})
    conn = open_db(tmp_path / "runtime.db")
    apply_migrations(conn, _RUNTIME)
    conn.close()
    before = _query(tmp_path / "runtime.db", "SELECT version FROM schema_migrations ORDER BY version")
    store = store_at(path)
    agg = store.tool("t")
    assert agg is not None and agg.invocations == 7
    after = _query(tmp_path / "runtime.db", "SELECT version FROM schema_migrations ORDER BY version")
    assert after == before
    assert path.is_file()


def test_get_affordances_on_a_fresh_tenant_leaves_no_runtime_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from recertia.api import create_app

    monkeypatch.delenv("RECERTIA_AFFORDANCE_SAMPLE_CAP", raising=False)
    root = tmp_path / "api-root"
    app = create_app(root=root, skills_root=tmp_path / "skills")
    issued = app.state.api_keys.issue(tenant_id="fresh", scopes={"runs"}, actor="test")
    resp = TestClient(app).get("/v1/affordances", headers={"X-API-Key": issued.secret})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"tools": [], "resources": []}
    assert not (root / "runs" / "fresh" / "runtime.db").exists()
    assert list(root.rglob("runs/fresh/runtime.db*")) == []


def test_memory_query_leaves_no_runtime_db(tmp_path: Path) -> None:
    from recertia.memory.procedural.store import SkillStore
    from recertia.memory.query import federated_query
    from recertia.retrieval.index import SkillIndex

    tenant = tmp_path / "runs" / "acme"
    for sub in ("facts", "episodic", "work"):
        (tenant / sub).mkdir(parents=True)
    skills = tmp_path / "skills"
    skills.mkdir()
    store = SkillStore(skills)
    index = SkillIndex(tenant / "skill_index.db")
    index.rebuild(store.iter_loaded(), library_fingerprint=store.library_fingerprint())
    index.close()
    payload = federated_query(
        "hello",
        skills_root=skills,
        facts_root=tenant / "facts",
        episodic_root=tenant / "episodic",
        index_path=tenant / "skill_index.db",
        workdir=tenant / "work",
        affordance_path=tenant / "affordances.json",
    )
    assert "error" not in payload
    assert payload["affordances"] == {"tools": [], "resources": []}
    assert list(tenant.glob("runtime.db*")) == []


# -- the first write imports once ------------------------------------------------------


def test_first_write_imports_legacy_json_once(tmp_path: Path, store_at) -> None:
    path = tmp_path / "affordances.json"
    _legacy(path, t={"invocations": 5, "failures": 1, "durations_s": [1.0], "costs_usd": [0.5]})
    store = store_at(path)
    store.record_tool(ToolResult(tool="t", ok=True, duration_s=2.0))
    assert not path.exists()
    assert (tmp_path / "affordances.json.imported").is_file()
    agg = store.tool("t")
    assert agg is not None and (agg.invocations, agg.failures) == (6, 1)
    assert agg.durations_s == [1.0, 2.0]
    db = tmp_path / "runtime.db"
    assert _query(db, "SELECT checksum FROM schema_migrations WHERE version=?", IMPORT_MARKER) == [
        ("imported",)
    ]
    # A second legacy file appearing later is not imported again.
    _legacy(path, t={"invocations": 100})
    again = store_at(path)
    again.save()
    agg = again.tool("t")
    assert agg is not None and agg.invocations == 6
    assert _query(db, "SELECT COUNT(*) FROM schema_migrations WHERE version=?", IMPORT_MARKER) == [(1,)]


def test_import_trims_legacy_lists_to_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store_at
) -> None:
    monkeypatch.setenv("RECERTIA_AFFORDANCE_SAMPLE_CAP", "3")
    path = tmp_path / "affordances.json"
    sigs = {f"s{i}": i for i in range(6)}
    _legacy(
        path,
        t={
            "invocations": 6,
            "durations_s": [float(i) for i in range(6)],
            "costs_usd": [0.0] * 6,
            "error_signatures": sigs,
        },
    )
    store = store_at(path)
    store.save()
    agg = store.tool("t")
    assert agg is not None
    assert agg.durations_s == [3.0, 4.0, 5.0]
    assert sorted(agg.error_signatures) == ["s3", "s4", "s5"]


def test_record_conflict_counts_and_caps_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store_at
) -> None:
    monkeypatch.setenv("RECERTIA_AFFORDANCE_SAMPLE_CAP", "2")
    store = store_at(tmp_path / "affordances.json")
    store.record_conflict(_conflict("8080", waited_ms=0))
    store.record_conflict(_conflict("8080", waited_ms=5, timed_out=True))
    store.record_conflict(_conflict("8080", waited_ms=7))
    res = store.resources[("service", "8080")]
    assert (res.conflicts, res.timeouts, res.observed_ceiling) == (3, 1, 1)
    assert res.waits_ms == [5, 7]


# -- caps --------------------------------------------------------------------------------


def test_samples_keep_the_newest_cap_per_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store_at
) -> None:
    monkeypatch.setenv("RECERTIA_AFFORDANCE_SAMPLE_CAP", "3")
    store = store_at(tmp_path / "affordances.json")
    for i in range(7):
        store.record_tool(ToolResult(tool="t", ok=True, duration_s=float(i), cost_usd=i / 10))
    store.record_tool(ToolResult(tool="u", ok=True, duration_s=9.0))
    agg = store.tools["t"]
    assert agg.invocations == 7
    assert agg.durations_s == [4.0, 5.0, 6.0]
    assert agg.costs_usd == [0.4, 0.5, 0.6]
    assert store.tools["u"].durations_s == [9.0]


def test_signature_cap_evicts_the_least_recently_seen_across_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store_at
) -> None:
    """``last_seq`` comes from the shared per-tool row, not from one store's own count."""

    monkeypatch.setenv("RECERTIA_AFFORDANCE_SAMPLE_CAP", "5")
    path = tmp_path / "affordances.json"
    first = store_at(path)
    second = store_at(path)
    for i in range(10):
        first.record_tool(ToolResult(tool="t", ok=False, error_signature=f"old{i}"))
    for i in range(3):
        second.record_tool(ToolResult(tool="t", ok=False, error_signature=f"new{i}"))
    sigs = set(first.tools["t"].error_signatures)
    assert sigs == {"old8", "old9", "new0", "new1", "new2"}
    assert len(sigs) == 5
    rows = _query(
        tmp_path / "runtime.db",
        "SELECT signature, last_seq FROM affordance_error_signatures ORDER BY last_seq",
    )
    assert rows == [("old8", 9), ("old9", 10), ("new0", 11), ("new1", 12), ("new2", 13)]


def test_a_repeated_signature_counts_up_and_stays(tmp_path: Path, monkeypatch, store_at) -> None:
    monkeypatch.setenv("RECERTIA_AFFORDANCE_SAMPLE_CAP", "2")
    store = store_at(tmp_path / "affordances.json")
    for sig in ("a", "b", "a", "c"):
        store.record_tool(ToolResult(tool="t", ok=False, error_signature=sig))
    assert store.tools["t"].error_signatures == {"a": 2, "c": 1}


@pytest.mark.parametrize("raw", ["0", "-1", "abc"])
def test_bad_sample_cap_is_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("RECERTIA_AFFORDANCE_SAMPLE_CAP", raw)
    with pytest.raises(ConfigError):
        AffordanceStore(tmp_path / "affordances.json")
    assert list(tmp_path.iterdir()) == []


def test_bad_sample_cap_does_not_fail_other_settings_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    from recertia.settings import load_settings

    monkeypatch.setenv("RECERTIA_AFFORDANCE_SAMPLE_CAP", "0")
    load_settings()


def test_sample_cap_is_read_once_at_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store_at
) -> None:
    monkeypatch.setenv("RECERTIA_AFFORDANCE_SAMPLE_CAP", "2")
    store = store_at(tmp_path / "affordances.json")
    monkeypatch.setenv("RECERTIA_AFFORDANCE_SAMPLE_CAP", "abc")
    for i in range(4):
        store.record_tool(ToolResult(tool="t", ok=True, duration_s=float(i)))
    assert store.tools["t"].durations_s == [2.0, 3.0]


# -- tenant ------------------------------------------------------------------------------


def test_tenant_comes_from_the_runs_directory(tmp_path: Path) -> None:
    db, tenant = locate_runtime_db(tmp_path / "root" / "runs" / "acme" / "affordances.json")
    assert (db, tenant) == (tmp_path / "root" / "runs" / "acme" / "runtime.db", "acme")


def test_invalid_tenant_directory_under_runs_is_a_config_error(tmp_path: Path) -> None:
    path = tmp_path / "root" / "runs" / "Bad Tenant!" / "affordances.json"
    with pytest.raises(ConfigError):
        locate_runtime_db(path)
    with pytest.raises(ConfigError):
        AffordanceStore(path)


def test_root_not_under_runs_is_the_local_tenant(tmp_path: Path, store_at) -> None:
    db, tenant = locate_runtime_db(tmp_path / "cli-root" / "affordances.json")
    assert (db, tenant) == (tmp_path / "cli-root" / "runtime.db", "local")
    store = store_at(tmp_path / "affordances.json")
    store.record_tool(ToolResult(tool="t", ok=True))
    assert _query(tmp_path / "runtime.db", "SELECT tenant_id FROM affordance_tools") == [("local",)]


# -- quarantine --------------------------------------------------------------------------


def test_corrupt_quarantine_never_overwrites_an_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    check_corrupt_quarantine_never_overwrites(tmp_path, monkeypatch)


def test_imported_rename_never_overwrites_an_existing_file(tmp_path: Path) -> None:
    check_imported_rename_never_overwrites(tmp_path)


# -- a failed write changes nothing -------------------------------------------------------


class _Deny:
    """Deny one action on one table on the store's connections once switched on.

    Re-installing the authorizer expires cached statements, so each switch makes
    SQLite prepare (and authorize) every statement again.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, action: int, table: str) -> None:
        self.action, self.table, self.on = action, table, False
        self.conns: list[sqlite3.Connection] = []
        real_open = affordance_mod.open_db

        def guarded(*args: object, **kwargs: object) -> sqlite3.Connection:
            conn = real_open(*args, **kwargs)  # type: ignore[arg-type]
            self.conns.append(conn)
            conn.set_authorizer(self._authorize)
            return conn

        monkeypatch.setattr(affordance_mod, "open_db", guarded)

    def _authorize(self, code: int, arg1: str | None, *_: object) -> int:
        if self.on and code == self.action and arg1 == self.table:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def arm(self, on: bool = True) -> None:
        self.on = on
        for conn in self.conns:
            try:
                conn.set_authorizer(self._authorize)
            except sqlite3.ProgrammingError:  # closed after a failed open
                pass


@pytest.mark.parametrize(
    ("action", "table"),
    [
        (sqlite3.SQLITE_DELETE, "affordance_tool_samples"),
        (sqlite3.SQLITE_INSERT, "affordance_tool_samples"),
    ],
    ids=["trim", "sample-insert"],
)
def test_a_failed_record_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store_at, action: int, table: str
) -> None:
    deny = _Deny(monkeypatch, action, table)
    store = store_at(tmp_path / "affordances.json")
    store.record_tool(ToolResult(tool="t", ok=True, duration_s=1.0, error_signature="x"))
    deny.arm()
    with pytest.raises(sqlite3.DatabaseError):
        store.record_tool(ToolResult(tool="t", ok=False, duration_s=2.0, error_signature="y"))
    deny.arm(False)
    agg = store.tools["t"]
    assert (agg.invocations, agg.failures) == (1, 0)
    assert agg.durations_s == [1.0]
    assert agg.error_signatures == {"x": 1}
    store.record_tool(ToolResult(tool="t", ok=True))
    assert store.tools["t"].invocations == 2


def test_a_failed_import_marker_rolls_the_import_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store_at
) -> None:
    db = tmp_path / "runtime.db"
    conn = open_db(db)
    apply_migrations(conn, _RUNTIME)
    conn.close()
    path = tmp_path / "affordances.json"
    _legacy(path, t={"invocations": 5})
    deny = _Deny(monkeypatch, sqlite3.SQLITE_INSERT, "schema_migrations")
    deny.on = True
    store = store_at(path)
    with pytest.raises(sqlite3.DatabaseError):
        store.record_tool(ToolResult(tool="t", ok=True))
    assert _query(db, "SELECT COUNT(*) FROM affordance_tools") == [(0,)]
    assert _query(db, "SELECT COUNT(*) FROM schema_migrations WHERE version=?", IMPORT_MARKER) == [(0,)]
    assert path.is_file()
    deny.arm(False)
    store.record_tool(ToolResult(tool="t", ok=True))
    agg = store.tools["t"]
    assert agg.invocations == 6


# -- a failed rename leaves nothing behind and loses no record ------------------------------


class _RefuseReplace:
    """Make ``os.replace`` raise a non-``FileNotFoundError`` ``OSError`` while on."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.on = True
        real = affordance_mod.os.replace

        def replace(src: object, dst: object) -> None:
            if self.on:
                raise PermissionError(13, "sharing violation (injected)", str(src))
            real(src, dst)  # type: ignore[arg-type]

        monkeypatch.setattr(affordance_mod.os, "replace", replace)


def test_a_failed_imported_rename_keeps_the_record_and_leaves_no_empty_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store_at
) -> None:
    path = tmp_path / "affordances.json"
    _legacy(path, t={"invocations": 5})
    refuse = _RefuseReplace(monkeypatch)
    store = store_at(path)
    store.record_tool(ToolResult(tool="t", ok=True))
    assert list(tmp_path.glob("affordances.json.*")) == []
    assert path.is_file()
    agg = store.tool("t")
    assert agg is not None and agg.invocations == 6
    refuse.on = False
    later = store_at(path)
    later.record_tool(ToolResult(tool="t", ok=True))
    assert not path.exists()
    assert (tmp_path / "affordances.json.imported").stat().st_size > 0
    agg = later.tool("t")
    assert agg is not None and agg.invocations == 7


def test_a_failed_corrupt_rename_rolls_back_and_leaves_no_empty_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store_at
) -> None:
    path = tmp_path / "affordances.json"
    path.write_text("{torn", encoding="utf-8")
    refuse = _RefuseReplace(monkeypatch)
    store = store_at(path)
    with pytest.raises(PermissionError):
        store.record_tool(ToolResult(tool="t", ok=True))
    assert list(tmp_path.glob("affordances.json.*")) == []
    assert path.read_text(encoding="utf-8") == "{torn"
    db = tmp_path / "runtime.db"
    assert _query(db, "SELECT COUNT(*) FROM schema_migrations WHERE version=?", IMPORT_MARKER) == [(0,)]
    refuse.on = False
    store.record_tool(ToolResult(tool="t", ok=True))
    assert [p.read_text(encoding="utf-8") for p in tmp_path.glob("affordances.json.corrupt-*")] == ["{torn"]
    agg = store.tool("t")
    assert agg is not None and agg.invocations == 1


# -- the read path surfaces database errors ------------------------------------------------


def test_a_corrupt_runtime_db_raises_on_read_instead_of_reading_empty(tmp_path: Path, store_at) -> None:
    path = tmp_path / "affordances.json"
    _legacy(path, t={"invocations": 3})
    writer = store_at(path)
    writer.record_tool(ToolResult(tool="t", ok=True))
    writer.close()
    db = tmp_path / "runtime.db"
    assert not path.exists()
    with db.open("r+b") as fh:
        fh.write(b"this is not an sqlite database header" * 4)
    reader = store_at(path)
    with pytest.raises(sqlite3.DatabaseError):
        reader.tool("t")


def test_a_runtime_db_no_writer_has_migrated_still_reads_legacy_json(tmp_path: Path, store_at) -> None:
    path = tmp_path / "affordances.json"
    _legacy(path, t={"invocations": 4})
    sqlite3.connect(tmp_path / "runtime.db").close()  # an empty database, no tables yet
    store = store_at(path)
    agg = store.tool("t")
    assert agg is not None and agg.invocations == 4


# -- every transaction takes the write lock up front ---------------------------------------


def test_the_import_transaction_holds_the_write_lock_from_its_first_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store_at
) -> None:
    """``BEGIN IMMEDIATE``: no other writer can start between the marker read and the
    import write, so concurrent first writes wait on the busy timeout instead of failing
    with ``SQLITE_BUSY`` on a read-to-write upgrade."""

    db = tmp_path / "runtime.db"
    probes: list[str] = []
    real_open = affordance_mod.open_db

    def traced(*args: object, **kwargs: object) -> sqlite3.Connection:
        conn = real_open(*args, **kwargs)  # type: ignore[arg-type]

        def on_statement(sql: str) -> None:
            if sql.startswith("SELECT 1 FROM schema_migrations") and IMPORT_MARKER in sql:
                other = sqlite3.connect(db, timeout=0, isolation_level=None)
                try:
                    other.execute("BEGIN IMMEDIATE")
                    other.execute("ROLLBACK")
                    probes.append("free")
                except sqlite3.OperationalError as exc:
                    probes.append(str(exc))
                finally:
                    other.close()

        conn.set_trace_callback(on_statement)
        return conn

    monkeypatch.setattr(affordance_mod, "open_db", traced)
    path = tmp_path / "affordances.json"
    _legacy(path, t={"invocations": 2})
    store = store_at(path)
    store.record_tool(ToolResult(tool="t", ok=True))
    assert probes == ["database is locked"]
    agg = store.tool("t")
    assert agg is not None and agg.invocations == 3
