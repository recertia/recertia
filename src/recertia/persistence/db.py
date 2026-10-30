"""One SQLite opener. Durability follows ``Settings.durability`` (D3)."""

from __future__ import annotations

import hashlib
import sqlite3
import time
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
        _set_journal_mode(conn, "MEMORY", timeout)
        conn.execute("PRAGMA synchronous=OFF")
    elif profile == "bulk":
        _set_journal_mode(conn, "WAL", timeout)
        conn.execute("PRAGMA synchronous=OFF")
    else:
        _set_journal_mode(conn, "WAL", timeout)
        sync = {"strict": "FULL", "normal": "NORMAL", "fast": "OFF"}[durability]
        conn.execute(f"PRAGMA synchronous={sync}")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database is busy" in message


def _set_journal_mode(conn: sqlite3.Connection, mode: str, timeout: float) -> None:
    """Set the journal mode, waiting up to ``timeout`` seconds while another opener holds the db.

    Switching a fresh file to WAL needs an exclusive lock, and SQLite answers a
    concurrent switch with SQLITE_BUSY without calling the busy handler, so two
    processes opening the same new file at once would fail here.
    """

    deadline = time.monotonic() + max(0.0, timeout)
    delay = 0.005
    while True:
        try:
            conn.execute(f"PRAGMA journal_mode={mode}")
            return
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc) or time.monotonic() >= deadline:
                raise
        time.sleep(delay)
        delay = min(delay * 2, 0.1)


def _migration_id(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("-- id:"):
            return line.split(":", 1)[1].strip()
    return path.name.split(".", 1)[0]


def _statements(script: str) -> list[str]:
    """Split a migration script into single statements for ``conn.execute``.

    ``executescript`` would COMMIT the open transaction first, so migrations run
    statement by statement inside the caller's ``BEGIN IMMEDIATE`` instead.
    """

    statements: list[str] = []
    start = 0
    for index, char in enumerate(script):
        if char == ";" and sqlite3.complete_statement(script[start : index + 1]):
            statement = script[start : index + 1].strip()
            if statement:
                statements.append(statement)
            start = index + 1
    tail = script[start:].strip()
    if tail:
        statements.append(tail)
    return statements


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


def _migration_files(directory: Path) -> list[tuple[Path, str, str]]:
    return [
        (path, _migration_id(path), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(directory.glob("*.sqlite.sql"))
    ]


def _recorded(conn: sqlite3.Connection) -> dict[str, str | None] | None:
    """``version -> checksum`` from ``schema_migrations``, or ``None`` when the table needs DDL."""

    columns = {row[1] for row in conn.execute("PRAGMA table_info(schema_migrations)")}
    if "version" not in columns or "checksum" not in columns:
        return None
    return {row[0]: row[1] for row in conn.execute("SELECT version, checksum FROM schema_migrations")}


def _checksum_error(version: str) -> MigrationChecksumError:
    return MigrationChecksumError(
        f"migration {version} checksum changed",
        details={"version": version},
    )


def _needs_write(conn: sqlite3.Connection, files: list[tuple[Path, str, str]]) -> bool:
    """Lock-free first look. ``False`` only when every file is recorded with its checksum.

    A changed checksum met before any pending write raises here, as the locked pass
    would; any pending write sends the caller to the locked pass, which decides again.
    """

    recorded = _recorded(conn)
    if recorded is None:
        return True
    for _path, version, digest in files:
        if version not in recorded or recorded[version] in (None, ""):
            return True
        if recorded[version] != digest:
            raise _checksum_error(version)
    return False


def apply_migrations(conn: sqlite3.Connection, directory: Path) -> list[str]:
    """Apply ``*.sqlite.sql`` files. Unchanged re-runs are no-ops.

    A pre-existing ``schema_migrations`` without ``checksum`` is backfilled from
    the current file bytes so databases created by the old runner still open.

    Atomic across processes: what to apply is decided again after ``BEGIN
    IMMEDIATE`` takes the write lock, and every statement and its
    ``schema_migrations`` row commit together, so concurrent openers of the same
    file apply each migration once. A changed checksum raises
    :class:`MigrationChecksumError`; files before it stay applied, nothing at or
    after it is.
    """

    files = _migration_files(directory)
    if conn.in_transaction:
        # Same as the old runner, whose ``executescript`` committed a pending transaction.
        conn.commit()
    if not _needs_write(conn, files):
        return []
    conn.execute("BEGIN IMMEDIATE")
    newly: list[str] = []
    mismatch: MigrationChecksumError | None = None
    try:
        _ensure_migrations_table(conn)
        applied_rows = {
            row[0]: row[1]
            for row in conn.execute("SELECT version, checksum FROM schema_migrations")
        }
        for path, version, digest in files:
            previous = applied_rows.get(version)
            if previous is None and version not in applied_rows:
                for statement in _statements(path.read_text(encoding="utf-8")):
                    conn.execute(statement)
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
                mismatch = _checksum_error(version)
                break
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    if mismatch is not None:
        raise mismatch
    return newly
