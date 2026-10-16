"""Checksummed migration runner."""

from __future__ import annotations

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
