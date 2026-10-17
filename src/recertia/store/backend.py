"""Driver-swap store backends: SQLite (default) and Postgres (optional psycopg)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from recertia.store import apply_sqlite_migrations, postgres_migration_sql, verify_sqlite_schema

Dialect = Literal["sqlite", "postgres"]


class StoreBackend(Protocol):
    dialect: Dialect

    def apply_migrations(self) -> list[str]: ...

    def table_names(self) -> set[str]: ...

    def close(self) -> None: ...


@dataclass
class SqliteBackend:
    path: Path
    dialect: Dialect = "sqlite"

    def apply_migrations(self) -> list[str]:
        return apply_sqlite_migrations(self.path)

    def table_names(self) -> set[str]:
        return verify_sqlite_schema(self.path)

    def close(self) -> None:
        return None


@dataclass
class PostgresBackend:
    """Postgres store for ``multi_node``.

    ``multi_node`` startup refuses sqlite and the local executor. This backend
    is optional in tests: skip when ``RECERTIA_TEST_PG_DSN`` is unset. Do not
    apply migrations from the default suite.
    """

    dsn: str
    dialect: Dialect = "postgres"
    _conn: object | None = None

    def connect(self) -> object:
        try:
            import psycopg  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("psycopg required for PostgresBackend") from exc
        self._conn = psycopg.connect(self.dsn)
        return self._conn

    def apply_migrations(self) -> list[str]:
        conn = self.connect()
        assert self._conn is not None
        sql = postgres_migration_sql()
        with conn.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
            cur.execute("SELECT version FROM schema_migrations")
            applied = {row[0] for row in cur.fetchall()}
            version = "001_init"
            newly: list[str] = []
            if version not in applied:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations(version) VALUES (%s) ON CONFLICT DO NOTHING",
                    (version,),
                )
                newly.append(version)
            conn.commit()  # type: ignore[attr-defined]
        return newly

    def table_names(self) -> set[str]:
        conn = self._conn or self.connect()
        with conn.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
            return {row[0] for row in cur.fetchall()}

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()  # type: ignore[attr-defined]
            self._conn = None


def open_backend(url: str | None = None, *, sqlite_path: Path | None = None) -> StoreBackend:
    """Open a backend from ``DATABASE_URL`` / ``url``, else local SQLite."""

    from recertia.settings import load_settings

    dsn = url or load_settings().database_url
    if dsn and dsn.startswith(("postgres://", "postgresql://")):
        return PostgresBackend(dsn=dsn)
    path = sqlite_path or Path(".recertia/store.sqlite")
    return SqliteBackend(path=path)


def postgres_dialect_mentions_pgvector() -> bool:
    return "vector" in postgres_migration_sql()
