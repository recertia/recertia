"""Checksummed migration runner."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

import pytest

from recertia.persistence.db import MigrationChecksumError, apply_migrations, open_db


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
