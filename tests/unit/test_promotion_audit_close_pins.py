"""Pins: the operator audit store closes its connection when a step after the open fails.

``__init__`` and ``record()`` turn the SQLite error into ``AuditSinkUnavailable``; ``read()``
lets it propagate. In each case ``close()`` ran exactly once on the connection ``_connect``
returned.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from recertia.review import promotion_audit
from recertia.review.promotion_audit import AuditSinkUnavailable, OperatorAuditStore


class _Spy:
    """A real connection that counts ``close()``; an ``execute`` containing ``fail_sql`` raises."""

    def __init__(self, conn: sqlite3.Connection, fail_sql: str, error: BaseException) -> None:
        self._conn = conn
        self._fail_sql = fail_sql
        self._error = error
        self.closes = 0
        self.failed = False

    def execute(self, sql: str, *args: Any) -> Any:
        if self._fail_sql in sql:
            self.failed = True
            raise self._error
        return self._conn.execute(sql, *args)

    def close(self) -> None:
        self.closes += 1
        self._conn.close()

    def __enter__(self) -> _Spy:
        self._conn.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._conn.__exit__(*exc)


def _spy_open_db(monkeypatch: pytest.MonkeyPatch, fail_sql: str, error: BaseException) -> list[_Spy]:
    real_open_db: Callable[..., sqlite3.Connection] = promotion_audit.open_db
    made: list[_Spy] = []

    def open_db(*args: Any, **kwargs: Any) -> _Spy:
        spy = _Spy(real_open_db(*args, **kwargs), fail_sql, error)
        made.append(spy)
        return spy

    monkeypatch.setattr(promotion_audit, "open_db", open_db)
    return made


def _entry() -> dict[str, Any]:
    return {"skill_id": "s", "version": 1, "refusal_kind": "fixtures_failed"}


def test_init_failure_after_open_closes_and_raises_sink_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = _spy_open_db(
        monkeypatch, "CREATE TABLE IF NOT EXISTS promotion_audit", sqlite3.OperationalError("disk full")
    )
    with pytest.raises(AuditSinkUnavailable):
        OperatorAuditStore(tmp_path / "operator")
    assert [(s.failed, s.closes) for s in made] == [(True, 1)]


def test_record_failure_after_open_closes_and_raises_sink_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = OperatorAuditStore(tmp_path / "operator")
    made = _spy_open_db(monkeypatch, "INSERT INTO promotion_audit", sqlite3.OperationalError("disk full"))
    with pytest.raises(AuditSinkUnavailable):
        store.record(_entry())
    assert [(s.failed, s.closes) for s in made] == [(True, 1)]


def test_read_failure_after_open_closes_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = OperatorAuditStore(tmp_path / "operator")
    audit_ref = store.record(_entry())
    error = sqlite3.OperationalError("database is locked")
    made = _spy_open_db(monkeypatch, "SELECT recorded_at, payload FROM promotion_audit", error)
    with pytest.raises(sqlite3.OperationalError) as excinfo:
        store.read(audit_ref)
    assert excinfo.value is error
    assert [(s.failed, s.closes) for s in made] == [(True, 1)]
