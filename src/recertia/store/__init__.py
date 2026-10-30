"""Versioned store migrations (SQLite for CI; Postgres + pgvector dialect shipped alongside)."""

from __future__ import annotations

from pathlib import Path

from recertia.persistence.db import apply_migrations, open_db

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


class MigrationError(RuntimeError):
    pass


def list_migrations(*, dialect: str = "sqlite") -> list[Path]:
    pattern = f"*.{dialect}.sql"
    return sorted(MIGRATIONS_DIR.glob(pattern))


def apply_sqlite_migrations(db_path: Path) -> list[str]:
    """Apply all sqlite migrations to ``db_path``; return the version ids this call applied.

    Runs :func:`recertia.persistence.db.apply_migrations` on ``MIGRATIONS_DIR``, so
    processes that open the same new file at once apply each migration exactly once.
    A ``schema_migrations`` table written by the earlier store runner (``version`` and
    ``applied_at`` only) gains a ``checksum`` column, filled from the current file bytes.
    The first call on such a db takes the write lock (``BEGIN IMMEDIATE``) once to add and
    fill the column; if another process holds a write transaction on the file, the call
    waits up to 5 s and then raises ``sqlite3.OperationalError: database is locked``.

    Raises :class:`recertia.persistence.db.MigrationChecksumError` (``state_corrupt``)
    when an applied migration file has changed, and
    :class:`recertia.persistence.db.MigrationError` when a file contains its own
    transaction control. This module's :class:`MigrationError` is a different class: it
    is only the "no postgres migrations found" error.
    """

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    try:
        return apply_migrations(conn, MIGRATIONS_DIR)
    finally:
        conn.close()


def postgres_migration_sql() -> str:
    """Return the concatenated Postgres+pgvector migration text (not executed here)."""

    parts = [p.read_text(encoding="utf-8") for p in list_migrations(dialect="postgres")]
    if not parts:
        raise MigrationError("no postgres migrations found")
    return "\n\n".join(parts)


def verify_sqlite_schema(db_path: Path) -> set[str]:
    conn = open_db(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()
