"""SkillIndex closes its connection when a post-connect setup step raises (REC-110).

The constructor opens through ``open_db_with_init``. The spy wraps the connection that
``open_db`` returns and counts ``close()`` directly. It patches ``open_db`` both where the
helper looks it up (``recertia.persistence.db``) and in ``recertia.retrieval.index``, so a
constructor that calls ``open_db`` itself is observed too.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from recertia.persistence import db as db_module
from recertia.persistence.db import open_db
from recertia.retrieval import index as index_module
from recertia.retrieval.index import SkillIndex


class _SetupFailed(Exception):
    pass


class _ConnSpy:
    """Fails statements containing ``fail_sql`` once ``open_db`` has handed the connection over."""

    def __init__(self, conn: sqlite3.Connection, fail_sql: str | None, error: BaseException) -> None:
        self._conn = conn
        self._fail_sql = fail_sql
        self._error = error
        self.closes = 0

    def close(self) -> None:
        self.closes += 1
        self._conn.close()

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        if self._fail_sql is not None and self._fail_sql in sql:
            raise self._error
        return self._conn.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class _OpenSpy:
    def __init__(self, fail_sql: str | None, error: BaseException) -> None:
        self.fail_sql = fail_sql
        self.error = error
        self.opened: list[_ConnSpy] = []

    def __call__(self, path: Path | str, **kwargs: Any) -> _ConnSpy:
        spy = _ConnSpy(open_db(path, **kwargs), self.fail_sql, self.error)
        self.opened.append(spy)
        return spy


def _install(monkeypatch: pytest.MonkeyPatch, spy: _OpenSpy) -> None:
    monkeypatch.setattr(db_module, "open_db", spy)
    monkeypatch.setattr(index_module, "open_db", spy, raising=False)


def test_first_setup_step_failure_closes_and_reraises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _OpenSpy("PRAGMA journal_mode=WAL", _SetupFailed("journal mode"))
    _install(monkeypatch, spy)
    with pytest.raises(_SetupFailed) as excinfo:
        SkillIndex(tmp_path / "index.sqlite")
    assert excinfo.value is spy.error
    assert [c.closes for c in spy.opened] == [1]


def test_init_schema_failure_closes_and_reraises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    error = _SetupFailed("schema")

    def failing_init_schema(self: SkillIndex) -> None:
        raise error

    spy = _OpenSpy(None, error)
    _install(monkeypatch, spy)
    monkeypatch.setattr(SkillIndex, "_init_schema", failing_init_schema)
    with pytest.raises(_SetupFailed) as excinfo:
        SkillIndex(tmp_path / "index.sqlite")
    assert excinfo.value is error
    assert [c.closes for c in spy.opened] == [1]


def test_real_sqlite_error_in_a_setup_step_closes_and_reraises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raised: list[sqlite3.OperationalError] = []

    def init_schema_on_a_missing_table(self: SkillIndex) -> None:
        try:
            self._conn.execute("SELECT skill_id FROM missing_table")
        except sqlite3.OperationalError as exc:
            raised.append(exc)
            raise

    spy = _OpenSpy(None, _SetupFailed("unused"))
    _install(monkeypatch, spy)
    monkeypatch.setattr(SkillIndex, "_init_schema", init_schema_on_a_missing_table)
    with pytest.raises(sqlite3.OperationalError, match="no such table") as excinfo:
        SkillIndex(tmp_path / "index.sqlite")
    assert len(raised) == 1 and excinfo.value is raised[0]
    assert [c.closes for c in spy.opened] == [1]


def test_successful_init_keeps_the_connection_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _OpenSpy(None, _SetupFailed("unused"))
    _install(monkeypatch, spy)
    index = SkillIndex(tmp_path / "index.sqlite")
    try:
        assert [c.closes for c in spy.opened] == [0]
        assert index._conn.execute("SELECT count(*) FROM skills").fetchone() == (0,)
    finally:
        index.close()
    assert [c.closes for c in spy.opened] == [1]
