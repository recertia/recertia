"""Pins for who closes the connection in ``require_lease_token``.

When the check opens ``run_leases`` itself it closes that connection exactly once, whether the
check passes, returns early (``missing_row='allow'`` with no ``run_leases`` table), rejects, or
fails on a query. A connection the caller passes in is never closed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from recertia.persistence import fence as fence_module
from recertia.persistence.fence import require_lease_token
from recertia.runtime.errors import LeaseLost

_LEASES = (
    "CREATE TABLE run_leases(tenant_id TEXT, run_id TEXT, fencing_token INTEGER, "
    "status TEXT, incarnation INTEGER)"
)


class _Spy:
    """A real connection that counts ``close()``; an ``execute`` containing ``fail_sql`` raises."""

    def __init__(self, conn: sqlite3.Connection, fail_sql: str | None = None) -> None:
        self._conn = conn
        self._fail_sql = fail_sql
        self.closes = 0

    def execute(self, sql: str, *args: Any) -> Any:
        if self._fail_sql is not None and self._fail_sql in sql:
            raise sqlite3.OperationalError("disk I/O error")
        return self._conn.execute(sql, *args)

    def close(self) -> None:
        self.closes += 1
        self._conn.close()


def _leases(path: Path, *, token: int = 5, status: str = "held", table: bool = True) -> Path:
    conn = sqlite3.connect(path)
    try:
        if table:
            conn.execute(_LEASES)
            conn.execute("INSERT INTO run_leases VALUES ('local', 'run-1', ?, ?, 1)", (token, status))
        else:
            conn.execute("CREATE TABLE unrelated(x INTEGER)")
        conn.commit()
    finally:
        conn.close()
    return path


def _spy_open_db(monkeypatch: pytest.MonkeyPatch, fail_sql: str | None = None) -> list[_Spy]:
    real_open_db: Callable[..., sqlite3.Connection] = fence_module.open_db
    made: list[_Spy] = []

    def open_db(*args: Any, **kwargs: Any) -> _Spy:
        spy = _Spy(real_open_db(*args, **kwargs), fail_sql)
        made.append(spy)
        return spy

    monkeypatch.setattr(fence_module, "open_db", open_db)
    return made


def _check(db: Path, **kwargs: Any) -> None:
    kwargs.setdefault("fencing_token", 5)
    require_lease_token(db, tenant_id="local", run_id="run-1", **kwargs)


def test_owned_connection_is_closed_once_on_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _leases(tmp_path / "runtime.db")
    made = _spy_open_db(monkeypatch)
    _check(db)
    assert [s.closes for s in made] == [1]


def test_owned_connection_is_closed_once_on_the_allow_early_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _leases(tmp_path / "runtime.db", table=False)
    made = _spy_open_db(monkeypatch)
    _check(db, fencing_token=None, missing_row="allow")
    assert [s.closes for s in made] == [1]


@pytest.mark.parametrize(
    ("leases", "kwargs"),
    [
        pytest.param({"token": 6}, {}, id="token-mismatch"),
        pytest.param({"status": "released"}, {}, id="released"),
        pytest.param({"table": False}, {}, id="no-table-reject"),
    ],
)
def test_owned_connection_is_closed_once_when_the_check_rejects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    leases: dict[str, Any],
    kwargs: dict[str, Any],
) -> None:
    db = _leases(tmp_path / "runtime.db", **leases)
    made = _spy_open_db(monkeypatch)
    with pytest.raises(LeaseLost):
        _check(db, **kwargs)
    assert [s.closes for s in made] == [1]


def test_owned_connection_is_closed_once_when_a_query_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _leases(tmp_path / "runtime.db")
    made = _spy_open_db(monkeypatch, fail_sql="FROM main.run_leases")
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        _check(db)
    assert [s.closes for s in made] == [1]


def test_caller_connection_is_not_closed_on_success(tmp_path: Path) -> None:
    db = _leases(tmp_path / "runtime.db")
    spy = _Spy(sqlite3.connect(db))
    try:
        _check(db, conn=spy)
        assert spy.closes == 0
    finally:
        spy._conn.close()


@pytest.mark.parametrize(
    ("leases", "fail_sql", "error"),
    [
        pytest.param({"token": 6}, None, LeaseLost, id="reject"),
        pytest.param({}, "FROM main.run_leases", sqlite3.OperationalError, id="query-error"),
    ],
)
def test_caller_connection_is_not_closed_on_failure(
    tmp_path: Path, leases: dict[str, Any], fail_sql: str | None, error: type[BaseException]
) -> None:
    db = _leases(tmp_path / "runtime.db", **leases)
    spy = _Spy(sqlite3.connect(db), fail_sql)
    try:
        with pytest.raises(error):
            _check(db, conn=spy)
        assert spy.closes == 0
    finally:
        spy._conn.close()
