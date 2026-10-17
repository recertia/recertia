"""One SQLite opener. Durability follows ``Settings.durability`` (D3)."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from recertia.errors import RecertiaError

DbProfile = Literal["durable", "bulk", "ephemeral"]
DurabilityName = Literal["strict", "normal", "fast"]


class MigrationChecksumError(RecertiaError):
    code = "state_corrupt"
    http_status = 503
    retryable = False
    exit_code = 7


def open_db(
    path: Path | str,
    *,
    profile: DbProfile = "durable",
    durability: DurabilityName = "normal",
    timeout: float = 5.0,
    check_same_thread: bool = False,
) -> sqlite3.Connection:
    """Open SQLite with WAL (or MEMORY) and the durability profile's synchronous mode."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout, check_same_thread=check_same_thread)
    if profile == "ephemeral":
        conn.execute("PRAGMA journal_mode=MEMORY")
        conn.execute("PRAGMA synchronous=OFF")
    elif profile == "bulk":
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=OFF")
    else:
        conn.execute("PRAGMA journal_mode=WAL")
        sync = {"strict": "FULL", "normal": "NORMAL", "fast": "OFF"}[durability]
        conn.execute(f"PRAGMA synchronous={sync}")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _migration_id(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("-- id:"):
            return line.split(":", 1)[1].strip()
    return path.name.split(".", 1)[0]


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(schema_migrations)")}
    if "checksum" not in columns:
        conn.execute("ALTER TABLE schema_migrations ADD COLUMN checksum TEXT")


def apply_migrations(conn: sqlite3.Connection, directory: Path) -> list[str]:
    """Apply ``*.sqlite.sql`` files. Unchanged re-runs are no-ops.

    A pre-existing ``schema_migrations`` without ``checksum`` is backfilled from
    the current file bytes so databases created by the old runner still open.
    """

    _ensure_migrations_table(conn)
    files = sorted(directory.glob("*.sqlite.sql"))
    applied_rows = {
        row[0]: row[1]
        for row in conn.execute("SELECT version, checksum FROM schema_migrations")
    }
    newly: list[str] = []
    for path in files:
        version = _migration_id(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        previous = applied_rows.get(version)
        if previous is None and version not in applied_rows:
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations(version, checksum, applied_at) VALUES (?, ?, ?)",
                (version, digest, datetime.now(timezone.utc).isoformat()),
            )
            newly.append(version)
            continue
        if previous in (None, ""):
            conn.execute(
                "UPDATE schema_migrations SET checksum=? WHERE version=?",
                (digest, version),
            )
            continue
        if previous != digest:
            raise MigrationChecksumError(
                f"migration {version} checksum changed",
                details={"version": version},
            )
    conn.commit()
    return newly
