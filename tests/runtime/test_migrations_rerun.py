"""Store migrations are idempotent and survive a crash between DDL and the version row.

apply_sqlite_migrations (src/recertia/store/__init__.py) runs executescript() -- which commits
on its own -- before INSERT INTO schema_migrations, so a crash in between leaves tables with no
version row. A rerun must then succeed, not fail with 'table already exists'.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from recertia.store import (
    apply_sqlite_migrations,
    list_migrations,
    verify_sqlite_schema,
)
from recertia.store.backend import open_backend


def test_rerun_is_noop(tmp_path: Path) -> None:
    db = tmp_path / "store.sqlite"
    first = apply_sqlite_migrations(db)
    assert first, "no migrations applied: are the .sql files packaged/discoverable?"
    assert first == [p.name.split(".")[0] for p in list_migrations(dialect="sqlite")]
    tables = verify_sqlite_schema(db)
    assert apply_sqlite_migrations(db) == []
    assert verify_sqlite_schema(db) == tables


def test_rerun_after_crash_before_version_row(tmp_path: Path) -> None:
    db = tmp_path / "store.sqlite"
    apply_sqlite_migrations(db)
    tables = verify_sqlite_schema(db)
    assert tables - {"schema_migrations"}, "migrations created no tables"
    with sqlite3.connect(db) as c:  # simulate: DDL committed, version row never written
        c.execute("DELETE FROM schema_migrations")
    assert apply_sqlite_migrations(db)  # re-applies without raising
    assert verify_sqlite_schema(db) == tables


def test_open_backend_defaults_to_sqlite_without_dsn(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    b = open_backend(sqlite_path=tmp_path / "s.sqlite")
    assert b.dialect == "sqlite"
    b.apply_migrations()
    assert b.apply_migrations() == []
