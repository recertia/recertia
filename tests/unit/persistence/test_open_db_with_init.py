"""open_db_with_init closes the connection when the owner's init step raises (REC-110)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from recertia.persistence import db as db_module
from recertia.persistence.db import open_db, open_db_with_init


class _InitFailed(Exception):
    pass


class _CloseSpy:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.closes = 0

    def close(self) -> None:
        self.closes += 1
        self.conn.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.conn, name)


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[_CloseSpy]:
    spies: list[_CloseSpy] = []

    def recording_open_db(path: Path | str, **kwargs: Any) -> _CloseSpy:
        spy = _CloseSpy(open_db(path, **kwargs))
        spies.append(spy)
        return spy

    monkeypatch.setattr(db_module, "open_db", recording_open_db)
    return spies


def test_exception_in_init_closes_once_and_reraises_the_same_object(
    tmp_path: Path, opened: list[_CloseSpy]
) -> None:
    error = _InitFailed("init failed")

    def init(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE t (x)")
        raise error

    with pytest.raises(_InitFailed) as excinfo:
        open_db_with_init(tmp_path / "a.db", init)
    assert excinfo.value is error
    assert [s.closes for s in opened] == [1]


def test_base_exception_in_init_closes_once_and_reraises(tmp_path: Path, opened: list[_CloseSpy]) -> None:
    error = KeyboardInterrupt()

    def init(conn: sqlite3.Connection) -> None:
        raise error

    with pytest.raises(KeyboardInterrupt) as excinfo:
        open_db_with_init(tmp_path / "b.db", init)
    assert excinfo.value is error
    assert [s.closes for s in opened] == [1]


def test_success_returns_the_connection_open_and_usable(tmp_path: Path, opened: list[_CloseSpy]) -> None:
    def init(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE t (x)")

    conn = open_db_with_init(tmp_path / "c.db", init)
    try:
        assert len(opened) == 1 and conn is opened[0]  # type: ignore[comparison-overlap]
        assert opened[0].closes == 0
        conn.execute("INSERT INTO t VALUES (1)")
        assert conn.execute("SELECT x FROM t").fetchall() == [(1,)]
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    finally:
        conn.close()
