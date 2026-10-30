"""Checksummed migration runner."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from recertia.persistence.db import MigrationChecksumError, MigrationError, apply_migrations, open_db

_REPO_SRC = Path(__file__).resolve().parents[3] / "src" / "recertia"


def test_rerun_is_noop(tmp_path: Path) -> None:
    directory = tmp_path / "migs"
    directory.mkdir()
    (directory / "001_baseline.sqlite.sql").write_text(
        "-- id: 001_baseline\nCREATE TABLE IF NOT EXISTS demo (id INTEGER);\n",
        encoding="utf-8",
    )
    conn = open_db(tmp_path / "runtime.db")
    try:
        first = apply_migrations(conn, directory)
        second = apply_migrations(conn, directory)
    finally:
        conn.close()
    assert first == ["001_baseline"]
    assert second == []


def test_checksum_mismatch_raises(tmp_path: Path) -> None:
    directory = tmp_path / "migs"
    directory.mkdir()
    path = directory / "001_baseline.sqlite.sql"
    path.write_text("-- id: 001_baseline\nCREATE TABLE IF NOT EXISTS demo (id INTEGER);\n", encoding="utf-8")
    conn = open_db(tmp_path / "runtime.db")
    try:
        apply_migrations(conn, directory)
        path.write_text(
            "-- id: 001_baseline\nCREATE TABLE IF NOT EXISTS demo (id INTEGER, extra TEXT);\n",
            encoding="utf-8",
        )
        with pytest.raises(MigrationChecksumError):
            apply_migrations(conn, directory)
    finally:
        conn.close()


def _write(directory: Path, name: str, body: str) -> Path:
    path = directory / f"{name}.sqlite.sql"
    path.write_text(f"-- id: {name}\n{body}", encoding="utf-8")
    return path


def _rows(db: Path) -> dict[str, str | None]:
    if "schema_migrations" not in _tables(db):
        return {}
    conn = sqlite3.connect(db)
    try:
        return {row[0]: row[1] for row in conn.execute("SELECT version, checksum FROM schema_migrations")}
    finally:
        conn.close()


def _tables(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def test_checksum_backfill_for_old_runner_table(tmp_path: Path) -> None:
    """A ``schema_migrations`` without ``checksum`` gains it, filled from the file bytes."""

    directory = tmp_path / "migs"
    directory.mkdir()
    path = _write(directory, "001_baseline", "CREATE TABLE IF NOT EXISTS demo (id INTEGER);\n")
    db = tmp_path / "runtime.db"
    old = sqlite3.connect(db)
    old.execute(
        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY,"
        " applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    old.execute("CREATE TABLE demo (id INTEGER)")
    old.execute("INSERT INTO schema_migrations(version) VALUES ('001_baseline')")
    old.commit()
    old.close()
    conn = open_db(db)
    try:
        assert apply_migrations(conn, directory) == []
        assert apply_migrations(conn, directory) == []
    finally:
        conn.close()
    assert _rows(db) == {"001_baseline": hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.mark.parametrize("blank", [None, ""])
def test_checksum_backfill_for_blank_checksum(tmp_path: Path, blank: str | None) -> None:
    directory = tmp_path / "migs"
    directory.mkdir()
    path = _write(directory, "001_baseline", "CREATE TABLE IF NOT EXISTS demo (id INTEGER);\n")
    db = tmp_path / "runtime.db"
    conn = open_db(db)
    try:
        assert apply_migrations(conn, directory) == ["001_baseline"]
        conn.execute("UPDATE schema_migrations SET checksum=?", (blank,))
        conn.commit()
        assert apply_migrations(conn, directory) == []
    finally:
        conn.close()
    assert _rows(db) == {"001_baseline": hashlib.sha256(path.read_bytes()).hexdigest()}


def test_checksum_mismatch_names_the_version_and_applies_nothing_after_it(tmp_path: Path) -> None:
    directory = tmp_path / "migs"
    directory.mkdir()
    first = _write(directory, "001_baseline", "CREATE TABLE IF NOT EXISTS demo (id INTEGER);\n")
    db = tmp_path / "runtime.db"
    conn = open_db(db)
    try:
        apply_migrations(conn, directory)
        digest = hashlib.sha256(first.read_bytes()).hexdigest()
        _write(directory, "001_baseline", "CREATE TABLE IF NOT EXISTS demo (id INTEGER, extra TEXT);\n")
        _write(directory, "002_later", "CREATE TABLE later (id INTEGER);\n")
        with pytest.raises(MigrationChecksumError) as excinfo:
            apply_migrations(conn, directory)
    finally:
        conn.close()
    assert excinfo.value.code == "state_corrupt"
    assert excinfo.value.details == {"version": "001_baseline"}
    assert str(excinfo.value) == "migration 001_baseline checksum changed"
    assert _rows(db) == {"001_baseline": digest}
    assert "later" not in _tables(db)


def test_checksum_mismatch_keeps_the_files_before_it(tmp_path: Path) -> None:
    """Files sorted before the changed one commit with their rows; the changed one stops the run."""

    directory = tmp_path / "migs"
    directory.mkdir()
    _write(directory, "002_changed", "CREATE TABLE IF NOT EXISTS demo (id INTEGER);\n")
    db = tmp_path / "runtime.db"
    conn = open_db(db)
    try:
        apply_migrations(conn, directory)
        _write(directory, "002_changed", "CREATE TABLE IF NOT EXISTS demo (id INTEGER, extra TEXT);\n")
        first = _write(directory, "001_new", "CREATE TABLE early (id INTEGER);\n")
        with pytest.raises(MigrationChecksumError):
            apply_migrations(conn, directory)
    finally:
        conn.close()
    assert _rows(db)["001_new"] == hashlib.sha256(first.read_bytes()).hexdigest()
    assert "early" in _tables(db)


def test_unchanged_rerun_does_not_wait_for_the_write_lock(tmp_path: Path) -> None:
    directory = tmp_path / "migs"
    directory.mkdir()
    _write(directory, "001_baseline", "CREATE TABLE IF NOT EXISTS demo (id INTEGER);\n")
    db = tmp_path / "runtime.db"
    conn = open_db(db)
    holder = open_db(db)
    try:
        apply_migrations(conn, directory)
        holder.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        assert apply_migrations(conn, directory) == []
        assert time.monotonic() - started < 2.0
    finally:
        holder.rollback()
        holder.close()
        conn.close()


def test_statements_run_one_by_one_inside_the_transaction(tmp_path: Path) -> None:
    """Semicolons in strings and comments do not split; two statements on a line do."""

    directory = tmp_path / "migs"
    directory.mkdir()
    _write(
        directory,
        "001_baseline",
        "-- a comment; with a semicolon\n"
        "CREATE TABLE a (v TEXT DEFAULT 'x;y'); CREATE TABLE b (id INTEGER);\n"
        "/* block; comment */\n"
        "INSERT INTO a DEFAULT VALUES;\n"
        "-- trailing comment\n",
    )
    db = tmp_path / "runtime.db"
    conn = open_db(db)
    try:
        assert apply_migrations(conn, directory) == ["001_baseline"]
        assert conn.execute("SELECT v FROM a").fetchall() == [("x;y",)]
    finally:
        conn.close()
    assert {"a", "b"} <= _tables(db)


def test_failed_migration_records_nothing(tmp_path: Path) -> None:
    """A statement that fails rolls back the whole run: no table, no row."""

    directory = tmp_path / "migs"
    directory.mkdir()
    _write(directory, "001_baseline", "CREATE TABLE good (id INTEGER);\nCREATE TABLE good (id INTEGER);\n")
    db = tmp_path / "runtime.db"
    conn = open_db(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(conn, directory)
        assert not conn.in_transaction
    finally:
        conn.close()
    assert "good" not in _tables(db)
    assert _rows(db) == {}


class _RacedConnection(sqlite3.Connection):
    """Lets another opener apply every migration just before this one takes the write lock."""

    race: tuple[Path, Path] | None = None

    def execute(self, sql: str, *args: object) -> sqlite3.Cursor:  # type: ignore[override]
        if sql == "BEGIN IMMEDIATE" and self.race is not None:
            db, directory = self.race
            self.race = None
            other = open_db(db)
            try:
                assert apply_migrations(other, directory) == ["001_baseline", "002_more"]
            finally:
                other.close()
        return super().execute(sql, *args)  # type: ignore[arg-type]


def test_decision_is_made_again_under_the_write_lock(tmp_path: Path) -> None:
    """The lock-free look sees pending files; another process applies them before the lock."""

    directory = tmp_path / "migs"
    directory.mkdir()
    _write(directory, "001_baseline", "CREATE TABLE demo (id INTEGER);\n")
    _write(directory, "002_more", "CREATE TABLE more (id INTEGER);\n")
    db = tmp_path / "runtime.db"
    conn = sqlite3.connect(db, timeout=5.0, factory=_RacedConnection)
    conn.race = (db, directory)
    try:
        assert apply_migrations(conn, directory) == []
        assert conn.race is None
    finally:
        conn.close()
    assert sorted(_rows(db)) == ["001_baseline", "002_more"]


def test_failed_migration_rolls_back_earlier_files_of_the_same_run(tmp_path: Path) -> None:
    """Unlike a checksum mismatch, a failing statement also undoes files applied before it in the run."""

    directory = tmp_path / "migs"
    directory.mkdir()
    _write(directory, "001_good", "CREATE TABLE good (id INTEGER);\n")
    _write(directory, "002_bad", "CREATE TABLE bad (id INTEGER);\nINSERT INTO missing VALUES (1);\n")
    db = tmp_path / "runtime.db"
    conn = open_db(db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such table: missing"):
            apply_migrations(conn, directory)
    finally:
        conn.close()
    assert not {"good", "bad"} & _tables(db)
    assert _rows(db) == {}


@pytest.mark.parametrize(
    ("statement", "keyword"),
    [
        ("COMMIT;", "COMMIT"),
        ("commit transaction;", "COMMIT"),
        ("BEGIN;", "BEGIN"),
        ("BEGIN IMMEDIATE;", "BEGIN"),
        ("END TRANSACTION;", "END"),
        ("ROLLBACK;", "ROLLBACK"),
        ("SAVEPOINT s;", "SAVEPOINT"),
        ("RELEASE s;", "RELEASE"),
        ("VACUUM;", "VACUUM"),
        ("PRAGMA journal_mode=DELETE;", "PRAGMA journal_mode"),
        ("pragma main . JOURNAL_MODE = wal;", "PRAGMA journal_mode"),
        ("-- note\n/* block */ COMMIT;", "COMMIT"),
    ],
)
def test_migration_with_its_own_transaction_control_is_refused(
    tmp_path: Path, statement: str, keyword: str
) -> None:
    """Checked before ``BEGIN IMMEDIATE``: nothing from any file of the run is applied."""

    directory = tmp_path / "migs"
    directory.mkdir()
    _write(directory, "001_first", "CREATE TABLE first (id INTEGER);\n")
    body = f"CREATE TABLE a (id INTEGER);\n{statement}\nCREATE TABLE b (id INTEGER);\n"
    _write(directory, "002_own_txn", body)
    db = tmp_path / "runtime.db"
    conn = open_db(db)
    try:
        with pytest.raises(MigrationError) as excinfo:
            apply_migrations(conn, directory)
        assert not conn.in_transaction
    finally:
        conn.close()
    assert excinfo.value.details == {"version": "002_own_txn", "keyword": keyword}
    assert not {"first", "a", "b"} & _tables(db)
    assert _rows(db) == {}


def test_transaction_words_inside_other_statements_are_allowed(tmp_path: Path) -> None:
    """A trigger's BEGIN ... END, names, strings and comments are not transaction control."""

    directory = tmp_path / "migs"
    directory.mkdir()
    _write(
        directory,
        "001_ok",
        "-- COMMIT; is only mentioned here\n"
        "CREATE TABLE commit_log (v TEXT DEFAULT 'ROLLBACK;');\n"
        "CREATE TABLE seen (n INTEGER);\n"
        "CREATE TRIGGER log_insert AFTER INSERT ON commit_log BEGIN\n"
        "  INSERT INTO seen VALUES (1); INSERT INTO seen VALUES (2);\n"
        "END;\n"
        "PRAGMA user_version = 3;\n",
    )
    db = tmp_path / "runtime.db"
    conn = open_db(db)
    try:
        assert apply_migrations(conn, directory) == ["001_ok"]
        conn.execute("INSERT INTO commit_log DEFAULT VALUES")
        assert conn.execute("SELECT count(*) FROM seen").fetchone() == (2,)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "directory",
    sorted({path.parent for path in _REPO_SRC.rglob("*.sqlite.sql")}),
    ids=lambda path: f"{path.parent.name}/{path.name}",
)
def test_repo_migrations_pass_the_transaction_guard(tmp_path: Path, directory: Path) -> None:
    conn = open_db(tmp_path / "runtime.db")
    try:
        assert apply_migrations(conn, directory)
        assert apply_migrations(conn, directory) == []
    finally:
        conn.close()


@pytest.mark.skipif(sys.version_info < (3, 12), reason="sqlite3 autocommit arrived in Python 3.12")
def test_autocommit_false_connection_gets_a_clear_type_error(tmp_path: Path) -> None:
    directory = tmp_path / "migs"
    directory.mkdir()
    _write(directory, "001_baseline", "CREATE TABLE demo (id INTEGER);\n")
    db = tmp_path / "runtime.db"
    options: dict[str, Any] = {"autocommit": False}
    conn = sqlite3.connect(db, **options)
    try:
        with pytest.raises(TypeError, match="autocommit=False"):
            apply_migrations(conn, directory)
    finally:
        conn.close()
    assert _rows(db) == {}


class _RollbackFails(sqlite3.Connection):
    def execute(self, sql: str, *args: object) -> sqlite3.Cursor:  # type: ignore[override]
        if sql == "ROLLBACK":
            raise sqlite3.OperationalError("rollback failed")
        return super().execute(sql, *args)  # type: ignore[arg-type]


def test_failing_rollback_does_not_hide_the_original_error(tmp_path: Path) -> None:
    directory = tmp_path / "migs"
    directory.mkdir()
    _write(directory, "001_bad", "CREATE TABLE a (id INTEGER);\nINSERT INTO missing VALUES (1);\n")
    db = tmp_path / "runtime.db"
    conn = sqlite3.connect(db, timeout=5.0, factory=_RollbackFails)
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such table: missing"):
            apply_migrations(conn, directory)
    finally:
        conn.close()  # closing with the transaction open rolls it back
    assert "a" not in _tables(db)
    assert _rows(db) == {}
