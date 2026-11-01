"""Nine more owners close their connection when a setup step fails.

Eight constructors and the skill-version allocator's ``_init_db`` open their database
through ``open_db_with_init`` and run their post-connect steps inside ``init``. Each case
wraps the connection that ``open_db`` returns for the owner's file and makes one setup step
raise: the first post-connect step, or the last one (after checking that the earlier steps
ran). The original error must propagate and ``close()`` must have run exactly once on the
connection the owner opened; ``_init_db`` returns no connection when it fails, so the spy is
the only handle on it. Counting ``close()`` directly keeps the test
independent of ``ResourceWarning`` (Python 3.12 does not emit one for an unclosed sqlite3
connection). A last case holds the runtime root lock so that ``OperationLedger`` raises
``RootBusy`` while it sets up its schema.
"""

from __future__ import annotations

import importlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from recertia.evals.store import EvalStore
from recertia.graph.ops import OperationLedger
from recertia.graph.store import CheckpointStore
from recertia.jobs.quota_repo import JobQuotaRepository
from recertia.memory.procedural import allocate
from recertia.persistence import db as db_module
from recertia.persistence.root_lock import RootBusy, RootLock
from recertia.persistence.sqlite.session import SqliteSessionRepo, SqliteTenantRepo
from recertia.runtime.registry import RunRegistry
from recertia.store.vectors import JsonBlobVectorIndex


class _SetupFailed(Exception):
    pass


@dataclass(frozen=True)
class Step:
    """One setup step to fail.

    ``kind`` is ``"set"`` (an attribute assignment on the connection), ``"execute"`` (an
    ``execute`` whose SQL contains ``detail``), ``"executescript"``, ``"commit"``, or
    ``"patch"`` (``detail`` names ``module:attribute`` or ``module:Class.attribute``, a function
    the owner calls with the connection, replaced by one that raises).
    """

    kind: str
    detail: str = ""

    def matches(self, kind: str, detail: str) -> bool:
        if kind != self.kind:
            return False
        if kind == "execute":
            return self.detail in detail
        if kind == "set":
            return self.detail == detail
        return True


class _ConnSpy:
    """A real connection that records each setup step, counts ``close()`` and fails one step."""

    def __init__(self, conn: sqlite3.Connection, fail: Step, error: BaseException) -> None:
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_fail", fail)
        object.__setattr__(self, "_error", error)
        object.__setattr__(self, "closes", 0)
        object.__setattr__(self, "steps", [])

    def _step(self, kind: str, detail: str) -> None:
        self.steps.append((kind, detail))
        if self._fail.matches(kind, detail):
            raise self._error

    def close(self) -> None:
        object.__setattr__(self, "closes", self.closes + 1)
        self._conn.close()

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        self._step("execute", sql.strip())
        return self._conn.execute(sql, *args)

    def executescript(self, script: str) -> sqlite3.Cursor:
        self._step("executescript", "")
        return self._conn.executescript(script)

    def commit(self) -> None:
        self._step("commit", "")
        self._conn.commit()

    def __enter__(self) -> _ConnSpy:
        self._conn.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._conn.__exit__(*exc)

    def __setattr__(self, name: str, value: Any) -> None:
        self._step("set", name)
        setattr(self._conn, name, value)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


@dataclass(frozen=True)
class Owner:
    construct: Callable[[Path], object]
    first: Step
    last: Step
    before_last: tuple[Step, ...]
    module: str = ""

    @property
    def module_name(self) -> str:
        return self.module or self.construct.__module__


WAL = Step("execute", "PRAGMA journal_mode=WAL")

OWNERS: dict[str, Owner] = {
    "EvalStore": Owner(
        EvalStore,
        Step("set", "row_factory"),
        Step("patch", "recertia.evals.store:EvalStore._init"),
        (Step("set", "row_factory"), WAL),
    ),
    "OperationLedger": Owner(
        OperationLedger, WAL, Step("patch", "recertia.graph.ops:_init_operations_table"), (WAL,)
    ),
    "OperationLedger[schema_lock=False]": Owner(
        lambda p: OperationLedger(p, schema_lock=False),
        WAL,
        Step("patch", "recertia.graph.ops:ensure_operations_schema"),
        (WAL,),
        module="recertia.graph.ops",
    ),
    "CheckpointStore": Owner(
        CheckpointStore,
        WAL,
        Step("patch", "recertia.graph.store:ensure_hop_tables"),
        (
            WAL,
            Step("execute", "CREATE TABLE IF NOT EXISTS checkpoints"),
            Step("commit"),
            Step("execute", "PRAGMA table_info(checkpoints)"),
            Step("execute", "CREATE TABLE IF NOT EXISTS eval_outbox"),
            Step("execute", "CREATE TABLE IF NOT EXISTS run_summaries"),
            Step("execute", "CREATE INDEX IF NOT EXISTS run_summaries_created"),
            Step("commit"),
        ),
    ),
    "JobQuotaRepository": Owner(
        JobQuotaRepository,
        Step("set", "isolation_level"),
        Step("patch", "recertia.jobs.quota_repo:apply_migrations"),
        (Step("set", "isolation_level"), Step("set", "row_factory")),
    ),
    "SqliteSessionRepo": Owner(
        SqliteSessionRepo, Step("executescript"), Step("commit"), (Step("executescript"),)
    ),
    "SqliteTenantRepo": Owner(
        SqliteTenantRepo, Step("executescript"), Step("commit"), (Step("executescript"),)
    ),
    "RunRegistry": Owner(
        RunRegistry,
        Step("set", "row_factory"),
        Step("execute", "CREATE INDEX IF NOT EXISTS runs_list"),
        (Step("set", "row_factory"), Step("execute", "CREATE TABLE IF NOT EXISTS runs")),
    ),
    "JsonBlobVectorIndex": Owner(
        JsonBlobVectorIndex, WAL, Step("commit"), (WAL, Step("execute", "CREATE TABLE IF NOT EXISTS vectors"))
    ),
    "allocate._init_db": Owner(
        allocate._init_db,
        Step("execute", "PRAGMA busy_timeout=30000"),
        Step("execute", "CREATE TABLE IF NOT EXISTS reservations"),
        (Step("execute", "PRAGMA busy_timeout=30000"), Step("set", "isolation_level")),
    ),
}


def _patch_target(detail: str) -> tuple[object, str]:
    module_name, _, attr = detail.partition(":")
    target: object = importlib.import_module(module_name)
    *owners, name = attr.split(".")
    for owner in owners:
        target = getattr(target, owner)
    return target, name


def _fail_setup_step(
    name: str, which: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[_ConnSpy, _SetupFailed, list[object]]:
    owner = OWNERS[name]
    step = owner.first if which == "first" else owner.last
    path = tmp_path / "owner.sqlite"
    error = _SetupFailed(f"{name}: {which} setup step failed")
    real_open_db = db_module.open_db
    spies: list[_ConnSpy] = []
    patched_calls: list[object] = []

    def open_db(db_path: Path | str, *args: Any, **kwargs: Any) -> Any:
        conn = real_open_db(db_path, *args, **kwargs)
        if Path(db_path) != path:
            return conn
        spy = _ConnSpy(conn, step, error)
        spies.append(spy)
        return spy

    # ``open_db_with_init`` looks ``open_db`` up in ``recertia.persistence.db``; an owner that
    # imported ``open_db`` itself is covered by patching its module too.
    monkeypatch.setattr(db_module, "open_db", open_db)
    module = importlib.import_module(owner.module_name)
    if hasattr(module, "open_db"):
        monkeypatch.setattr(module, "open_db", open_db)
    if step.kind == "patch":
        target, attr = _patch_target(step.detail)

        def failing(conn: Any, *args: Any, **kwargs: Any) -> None:
            patched_calls.append(conn)
            raise error

        if isinstance(target, type):
            monkeypatch.setattr(target, attr, lambda self, *a, **k: failing(self._conn, *a, **k))
        else:
            monkeypatch.setattr(target, attr, failing)

    with pytest.raises(_SetupFailed) as excinfo:
        owner.construct(path)

    assert excinfo.value is error
    assert len(spies) == 1, f"{name} opened {len(spies)} connections to its file"
    spy = spies[0]
    if step.kind == "patch":
        assert patched_calls == [spy], f"{name}: {step.detail} called with {patched_calls}"
        earlier = spy.steps
    else:
        assert step.matches(*spy.steps[-1]), f"{name}: the setup did not stop at {step}: {spy.steps}"
        earlier = spy.steps[:-1]
    if which == "first":
        assert earlier == [], f"{name}: {step} is not the first post-connect step: {spy.steps}"
    else:
        _assert_ran_in_order(name, owner.before_last, earlier)
    return spy, error, patched_calls


def _assert_ran_in_order(name: str, expected: tuple[Step, ...], steps: list[tuple[str, str]]) -> None:
    """Every expected earlier step ran, in this order, before the last step failed."""

    remaining = iter(steps)
    for want in expected:
        assert any(want.matches(*got) for got in remaining), (
            f"{name}: {want} did not run before the last step: {steps}"
        )


@pytest.mark.parametrize("name", sorted(OWNERS))
def test_first_setup_step_fails_closes_connection_once(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy, _, _ = _fail_setup_step(name, "first", tmp_path, monkeypatch)
    assert spy.closes == 1, f"{name} closed its connection {spy.closes} times"
    with pytest.raises(sqlite3.ProgrammingError):
        spy._conn.execute("SELECT 1")


@pytest.mark.parametrize("name", sorted(OWNERS))
def test_last_setup_step_fails_closes_connection_once(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy, _, _ = _fail_setup_step(name, "last", tmp_path, monkeypatch)
    assert spy.closes == 1, f"{name} closed its connection {spy.closes} times"
    with pytest.raises(sqlite3.ProgrammingError):
        spy._conn.execute("SELECT 1")


def test_operation_ledger_root_busy_closes_connection_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held root lock makes the schema setup raise ``RootBusy``; the connection is closed."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.2")
    path = tmp_path / "operations.db"
    real_open_db = db_module.open_db
    spies: list[_ConnSpy] = []

    def open_db(db_path: Path | str, *args: Any, **kwargs: Any) -> Any:
        conn = real_open_db(db_path, *args, **kwargs)
        if Path(db_path) != path:
            return conn
        spy = _ConnSpy(conn, Step("none"), _SetupFailed("unused"))
        spies.append(spy)
        return spy

    monkeypatch.setattr(db_module, "open_db", open_db)
    ops_module = importlib.import_module(OperationLedger.__module__)
    if hasattr(ops_module, "open_db"):
        monkeypatch.setattr(ops_module, "open_db", open_db)

    lock = RootLock(tmp_path / ".lock")
    lock.try_exclusive()
    try:
        with pytest.raises(RootBusy):
            OperationLedger(path, schema_lock=True)
    finally:
        lock.release()

    assert len(spies) == 1
    spy = spies[0]
    assert spy.closes == 1, f"OperationLedger closed its connection {spy.closes} times"
    with pytest.raises(sqlite3.ProgrammingError):
        spy._conn.execute("SELECT 1")
