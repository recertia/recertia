"""Versioned store migrations (SQLite for CI; Postgres + pgvector dialect shipped alongside)."""

from __future__ import annotations

from pathlib import Path

from recertia.persistence.db import open_db

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


class MigrationError(RuntimeError):
    pass


def list_migrations(*, dialect: str = "sqlite") -> list[Path]:
    pattern = f"*.{dialect}.sql"
    return sorted(MIGRATIONS_DIR.glob(pattern))


def apply_sqlite_migrations(db_path: Path) -> list[str]:
    """Apply all sqlite migrations to ``db_path``; return applied version ids."""

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        applied = {
            row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        newly: list[str] = []
        for path in list_migrations(dialect="sqlite"):
            version = path.name.split(".")[0]
            if version in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
            newly.append(version)
        conn.commit()
        return newly
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
