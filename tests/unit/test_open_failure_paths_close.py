"""Failure exits of the connections the open-call guard allowlists.

Each case wraps the real connection in a spy that counts ``close()`` and makes one step
after the open fail. Counting ``close()`` directly keeps the tests independent of
``ResourceWarning`` (Python 3.12 does not emit one for an unclosed sqlite3 connection).

- First-use writers (``AffordanceStore._write_conn``, ``IdempotencyStore._writer``): a setup
  step raises; ``close()`` runs once and the same error propagates.
- Read-only probes in ``import_legacy``: the query raises ``sqlite3.Error``; ``close()`` runs
  once and the probe returns ``False``.
- ``AffordanceStore._open_reader``: every exit after the connect closes the connection, and
  ``AffordanceStore._read`` closes the one it gets back.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import recertia.memory.affordance as affordance_module
from recertia.memory.affordance import AffordanceStore
from recertia.persistence import idempotency as idempotency_module
from recertia.persistence import import_legacy
from recertia.persistence.idempotency import IdempotencyStore


class _Spy:
    """A real connection that counts ``close()`` and can fail one step.

    ``fail_sql``: an ``execute`` whose SQL contains this text raises ``error``.
    ``fail_set``: assigning this attribute raises ``error``.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        fail_sql: str | None = None,
        fail_set: str | None = None,
        error: BaseException | None = None,
    ) -> None:
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_fail_sql", fail_sql)
        object.__setattr__(self, "_fail_set", fail_set)
        object.__setattr__(self, "_error", error)
        object.__setattr__(self, "closes", 0)
        object.__setattr__(self, "executed", [])

    def execute(self, sql: str, *args: Any) -> Any:
        self.executed.append(sql)
        if self._fail_sql is not None and self._fail_sql in sql:
            assert self._error is not None
            raise self._error
        return self._conn.execute(sql, *args)

    def close(self) -> None:
        object.__setattr__(self, "closes", self.closes + 1)
        self._conn.close()

    def __setattr__(self, name: str, value: Any) -> None:
        if name == self._fail_set:
            assert self._error is not None
            raise self._error
        setattr(self._conn, name, value)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def __enter__(self) -> _Spy:
        self._conn.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._conn.__exit__(*exc)


def _spy_connect(monkeypatch: pytest.MonkeyPatch, module: Any, **spy: Any) -> list[_Spy]:
    """Route ``module.sqlite3.connect`` through spies; the last spy made gets ``spy``'s failure."""

    real_connect = sqlite3.connect
    made: list[_Spy] = []

    def connect(*args: Any, **kwargs: Any) -> _Spy:
        conn = _Spy(real_connect(*args, **kwargs), **spy)
        made.append(conn)
        return conn

    monkeypatch.setattr(module.sqlite3, "connect", connect)
    return made


def _db(path: Path, *ddl: str) -> Path:
    conn = sqlite3.connect(path)
    try:
        for statement in ddl:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()
    return path


class _SetupFailed(Exception):
    pass


# -- first-use writers -----------------------------------------------------------------------


def _spy_open_db(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[_Spy]:
    real_open_db: Callable[..., sqlite3.Connection] = module.open_db
    made: list[_Spy] = []

    def open_db(*args: Any, **kwargs: Any) -> _Spy:
        conn = _Spy(real_open_db(*args, **kwargs))
        made.append(conn)
        return conn

    monkeypatch.setattr(module, "open_db", open_db)
    return made


def _failing(error: BaseException) -> Callable[..., None]:
    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise error

    return fail


def test_affordance_writer_setup_failure_closes_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AffordanceStore(tmp_path / "affordances.json")
    made = _spy_open_db(monkeypatch, affordance_module)
    error = _SetupFailed("migrations failed")
    monkeypatch.setattr(affordance_module, "apply_migrations", _failing(error))
    with pytest.raises(_SetupFailed) as excinfo:
        store._write_conn()
    assert excinfo.value is error
    assert [c.closes for c in made] == [1]
    assert store._writer is None


def test_idempotency_writer_setup_failure_closes_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = IdempotencyStore(tmp_path / "control.db")
    made = _spy_open_db(monkeypatch, idempotency_module)
    error = _SetupFailed("migrations failed")
    monkeypatch.setattr(idempotency_module, "apply_migrations", _failing(error))
    with pytest.raises(_SetupFailed) as excinfo:
        store._writer()
    assert excinfo.value is error
    assert [c.closes for c in made] == [1]
    assert store._conn is None


# -- import_legacy read-only probes ----------------------------------------------------------


def test_legacy_import_complete_probe_closes_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path / "runtime.db", "CREATE TABLE schema_migrations(version TEXT)")
    made = _spy_connect(
        monkeypatch,
        import_legacy,
        fail_sql="FROM schema_migrations",
        error=sqlite3.OperationalError("disk I/O error"),
    )
    assert import_legacy.legacy_import_complete(db) is False
    assert [c.closes for c in made] == [1]
    assert any("FROM schema_migrations" in sql for sql in made[0].executed)


def test_sqlite_has_table_probe_closes_on_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db(tmp_path / "checkpoints.sqlite", "CREATE TABLE checkpoints(run_id TEXT)")
    made = _spy_connect(
        monkeypatch,
        import_legacy,
        fail_sql="FROM sqlite_master",
        error=sqlite3.DatabaseError("database disk image is malformed"),
    )
    assert import_legacy.sqlite_has_table(db, "checkpoints") is False
    assert [c.closes for c in made] == [1]
    assert any("FROM sqlite_master" in sql for sql in made[0].executed)


def test_checkpoint_has_run_probe_closes_on_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db(tmp_path / "checkpoints.sqlite", "CREATE TABLE checkpoints(run_id TEXT)")
    # The first connect is sqlite_has_table's check, which passes; the second is the probe.
    made = _spy_connect(
        monkeypatch,
        import_legacy,
        fail_sql="FROM checkpoints WHERE run_id",
        error=sqlite3.OperationalError("disk I/O error"),
    )
    assert import_legacy.checkpoint_has_run(db, "run-1") is False
    assert [c.closes for c in made] == [1, 1]
    assert any("FROM checkpoints WHERE run_id" in sql for sql in made[1].executed)


# -- AffordanceStore._open_reader ------------------------------------------------------------


def _reader_store(tmp_path: Path) -> AffordanceStore:
    store = AffordanceStore(tmp_path / "affordances.json")
    _db(store.runtime_db, "CREATE TABLE unrelated(x INTEGER)")
    return store


def test_affordance_reader_isolation_level_failure_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _reader_store(tmp_path)
    error = sqlite3.ProgrammingError("cannot set isolation_level")
    made = _spy_connect(monkeypatch, affordance_module, fail_set="isolation_level", error=error)
    with pytest.raises(sqlite3.ProgrammingError) as excinfo:
        store._open_reader()
    assert excinfo.value is error
    assert [c.closes for c in made] == [1]


def test_affordance_reader_operational_error_closes_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _reader_store(tmp_path)
    error = sqlite3.OperationalError("database is locked")
    made = _spy_connect(monkeypatch, affordance_module, fail_sql="FROM schema_migrations", error=error)
    with pytest.raises(sqlite3.OperationalError) as excinfo:
        store._open_reader()
    assert excinfo.value is error
    assert [c.closes for c in made] == [1]


def test_affordance_reader_no_such_table_closes_and_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A database no writer has migrated: the real query fails with "no such table".
    store = _reader_store(tmp_path)
    made = _spy_connect(monkeypatch, affordance_module)
    assert store._open_reader() is None
    assert [c.closes for c in made] == [1]
    assert any("FROM schema_migrations" in sql for sql in made[0].executed)


def test_affordance_reader_other_error_closes_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _reader_store(tmp_path)
    error = sqlite3.DatabaseError("database disk image is malformed")
    made = _spy_connect(monkeypatch, affordance_module, fail_sql="FROM schema_migrations", error=error)
    with pytest.raises(sqlite3.DatabaseError) as excinfo:
        store._open_reader()
    assert excinfo.value is error
    assert [c.closes for c in made] == [1]


def test_affordance_read_closes_the_reader_it_gets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writer = AffordanceStore(tmp_path / "affordances.json")
    writer._write_conn()
    writer.close()
    store = AffordanceStore(tmp_path / "affordances.json")
    made = _spy_connect(monkeypatch, affordance_module)
    assert store.tools == {}
    assert [c.closes for c in made] == [1]
    # The reader was handed to _read (only _read begins a transaction on it).
    assert "BEGIN" in made[0].executed
