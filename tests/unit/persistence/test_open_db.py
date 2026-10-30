"""open_db durability pragmas, and the bounded wait for the journal-mode switch."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from recertia.persistence import db as db_module
from recertia.persistence.db import open_db


def _sync(path: Path, durability: str) -> str:
    conn = open_db(path, durability=durability)  # type: ignore[arg-type]
    try:
        row = conn.execute("PRAGMA synchronous").fetchone()
        assert row is not None
        # 0 OFF, 1 NORMAL, 2 FULL
        return {0: "OFF", 1: "NORMAL", 2: "FULL"}[int(row[0])]
    finally:
        conn.close()


def test_durable_normal_sets_synchronous_normal(tmp_path: Path) -> None:
    assert _sync(tmp_path / "n.db", "normal") == "NORMAL"


def test_durable_strict_sets_full(tmp_path: Path) -> None:
    assert _sync(tmp_path / "s.db", "strict") == "FULL"


def test_fast_sets_off(tmp_path: Path) -> None:
    assert _sync(tmp_path / "f.db", "fast") == "OFF"


# --- the journal-mode wait (db._set_journal_mode) -------------------------------------
#
# These tests never wait on a real clock. ``_Clock`` replaces ``time`` inside db.py, and
# ``_FlakyJournalMode`` makes the first journal-mode PRAGMAs raise. The busy error it
# raises is a real SQLITE_BUSY, taken from a connection blocked by a held lock.


def _real_busy_error(tmp_path: Path) -> sqlite3.OperationalError:
    path = tmp_path / "held.db"
    holder = sqlite3.connect(path, isolation_level=None)
    blocked = sqlite3.connect(path, timeout=0)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            blocked.execute("PRAGMA journal_mode=WAL")
    finally:
        blocked.close()
        holder.close()
    assert excinfo.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
    return excinfo.value


class _Clock:
    """Stands in for the ``time`` module inside db.py: ``sleep`` advances the clock at once."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _FlakyJournalMode:
    """Patches ``sqlite3.connect`` so the n-th journal-mode PRAGMA raises ``fail(n)`` if not None."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, fail: Callable[[int], Exception | None]) -> None:
        self.calls = 0
        self.connections: list[sqlite3.Connection] = []
        flaky = self

        class _Connection(sqlite3.Connection):
            def execute(self, sql: str, *args: object) -> sqlite3.Cursor:  # type: ignore[override]
                if sql.startswith("PRAGMA journal_mode="):
                    flaky.calls += 1
                    error = fail(flaky.calls)
                    if error is not None:
                        raise error
                return super().execute(sql, *args)  # type: ignore[arg-type]

        real_connect = sqlite3.connect

        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            conn = real_connect(*args, factory=_Connection, **kwargs)
            self.connections.append(conn)
            return conn

        monkeypatch.setattr(db_module.sqlite3, "connect", connect)

    def close(self) -> None:
        for conn in self.connections:
            conn.close()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(db_module, "time", fake)
    return fake


def test_busy_open_gives_up_after_its_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    busy = _real_busy_error(tmp_path)
    flaky = _FlakyJournalMode(monkeypatch, lambda _n: busy)
    try:
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            open_db(tmp_path / "a.db", timeout=0.6)
    finally:
        flaky.close()
    assert excinfo.value is busy
    assert flaky.calls > 1, "a busy journal-mode switch must be retried"
    assert flaky.calls == len(clock.sleeps) + 1
    # It gives up at the deadline, not before, and at most one capped backoff past it.
    assert 0.6 <= sum(clock.sleeps) <= 0.6 + 0.1
    assert max(clock.sleeps) <= 0.1


def test_busy_open_succeeds_once_the_lock_is_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    busy = _real_busy_error(tmp_path)
    flaky = _FlakyJournalMode(monkeypatch, lambda n: busy if n <= 3 else None)
    try:
        conn = open_db(tmp_path / "b.db", timeout=5.0)
        assert flaky.calls == 4
        assert len(clock.sleeps) == 3
        row = conn.execute("PRAGMA journal_mode").fetchone()
        assert row is not None and row[0] == "wal"
    finally:
        flaky.close()


def _coded(error: sqlite3.Error, code: int) -> sqlite3.Error:
    error.sqlite_errorcode = code
    return error


@pytest.mark.parametrize(
    "error",
    [
        sqlite3.OperationalError("disk I/O error"),
        _coded(sqlite3.OperationalError("disk I/O error"), sqlite3.SQLITE_IOERR),
        _coded(sqlite3.OperationalError("database table is locked"), sqlite3.SQLITE_LOCKED),
        _coded(sqlite3.DatabaseError("file is not a database"), sqlite3.SQLITE_NOTADB),
    ],
    ids=["io-no-code", "io", "table-locked", "not-a-database"],
)
def test_non_busy_error_comes_straight_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock, error: sqlite3.Error
) -> None:
    flaky = _FlakyJournalMode(monkeypatch, lambda _n: error)
    try:
        with pytest.raises(type(error)) as excinfo:
            open_db(tmp_path / "c.db", timeout=5.0)
    finally:
        flaky.close()
    assert excinfo.value is error
    assert flaky.calls == 1
    assert clock.sleeps == []
