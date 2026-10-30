"""REC-056d-pre2: ``apply_sqlite_migrations`` runs the shared ``persistence.db`` runner.

The error classes come from ``recertia.persistence.db``; ``recertia.store.MigrationError``
is a different class (the Postgres "no migrations found" error).
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

import recertia.store as store
from recertia.persistence import db as persistence_db
from recertia.persistence.db import MigrationChecksumError, MigrationError
from recertia.store import apply_sqlite_migrations, list_migrations
from tests.support.store_migrations import check_concurrent_existing_store_dbs_apply_an_added_migration_once


def _copy_migrations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    copy = tmp_path / "migrations"
    shutil.copytree(store.MIGRATIONS_DIR, copy)
    monkeypatch.setattr(store, "MIGRATIONS_DIR", copy)
    return copy


def _disk(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir())}


def _dump(db: Path) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


def test_store_migration_ids_are_the_ones_the_earlier_runner_recorded() -> None:
    files = list_migrations(dialect="sqlite")
    assert files
    for path in files:
        assert persistence_db._migration_id(path) == path.name.split(".")[0], path.name


def test_a_changed_store_migration_raises_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = _copy_migrations(tmp_path, monkeypatch)
    db = tmp_path / "db" / "store.sqlite"
    assert apply_sqlite_migrations(db) == ["001_init"]
    target = copy / "001_init.sqlite.sql"
    data = bytearray(target.read_bytes())
    data[3] ^= 0x01  # one byte of the leading comment
    target.write_bytes(bytes(data))
    disk, dump = _disk(db.parent), _dump(db)

    with pytest.raises(MigrationChecksumError) as caught:
        apply_sqlite_migrations(db)

    assert caught.value.code == "state_corrupt"
    assert caught.value.details == {"version": "001_init"}
    assert _disk(db.parent) == disk
    assert _dump(db) == dump


def test_a_store_migration_with_its_own_commit_is_refused_before_anything_is_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = _copy_migrations(tmp_path, monkeypatch)
    (copy / "002_own_commit.sqlite.sql").write_text(
        "CREATE TABLE t (x INTEGER);\nCOMMIT;\n", encoding="utf-8"
    )
    db = tmp_path / "db" / "store.sqlite"

    with pytest.raises(MigrationError) as caught:
        apply_sqlite_migrations(db)

    assert not isinstance(caught.value, store.MigrationError)
    assert caught.value.details == {"version": "002_own_commit", "keyword": "COMMIT"}
    assert store.verify_sqlite_schema(db) == set()


def test_existing_store_dbs_apply_an_added_migration_once_across_processes(tmp_path, monkeypatch):
    check_concurrent_existing_store_dbs_apply_an_added_migration_once(tmp_path, monkeypatch)
