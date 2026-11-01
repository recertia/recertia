"""One SQLite opener. Durability follows ``Settings.durability`` (D3)."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from collections.abc import Callable
from contextlib import suppress
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


class MigrationError(RecertiaError):
    """A migration file the runner refuses to apply, for example one with its own ``COMMIT``."""


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
    try:
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
    except BaseException:
        # Nothing else holds the connection yet; leaving it open leaks the file handle.
        # A failing close must not replace the error being raised.
        with suppress(Exception):
            conn.close()
        raise
    return conn


def open_db_with_init(
    path: Path | str,
    init: Callable[[sqlite3.Connection], None],
    *,
    profile: DbProfile = "durable",
    durability: DurabilityName = "normal",
    timeout: float = 5.0,
    check_same_thread: bool = False,
) -> sqlite3.Connection:
    """``open_db``, then ``init(conn)``; if ``init`` raises anything, close the connection.

    For an owner that keeps the connection (a constructor): until ``init`` returns, nothing
    else holds the connection, so on any ``BaseException`` it is closed and the original
    exception re-raised. On success the open connection is returned.
    """

    conn = open_db(
        path,
        profile=profile,
        durability=durability,
        timeout=timeout,
        check_same_thread=check_same_thread,
    )
    try:
        init(conn)
    except BaseException:
        # A failing close must not replace the error ``init`` raised.
        with suppress(Exception):
            conn.close()
        raise
    return conn


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    """SQLITE_BUSY, including its extended codes. Python 3.11+ sets ``sqlite_errorcode``."""

    code = getattr(exc, "sqlite_errorcode", None)
    return isinstance(code, int) and code & 0xFF == sqlite3.SQLITE_BUSY


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


_LEADING_TRIVIA = re.compile(r"(?:\s+|--[^\n]*(?:\n|$)|/\*.*?(?:\*/|$))*", re.DOTALL)
_FIRST_WORD = re.compile(r"[A-Za-z_]+")
_PRAGMA_JOURNAL_MODE = re.compile(r"PRAGMA\s+(?:[\w\"`\[\]]+\s*\.\s*)?journal_mode\b", re.IGNORECASE)
_TRANSACTION_KEYWORDS = frozenset(
    {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE", "VACUUM"}
)


def _forbidden_keyword(statement: str) -> str | None:
    """The transaction-control keyword a statement starts with, if any.

    The runner owns the transaction, so a migration's own ``COMMIT`` would split
    it and ``VACUUM`` or a journal-mode switch cannot run inside it.
    """

    match = _LEADING_TRIVIA.match(statement)
    body = statement[match.end() if match else 0 :]
    if _PRAGMA_JOURNAL_MODE.match(body):
        return "PRAGMA journal_mode"
    word = _FIRST_WORD.match(body)
    if word and word.group(0).upper() in _TRANSACTION_KEYWORDS:
        return word.group(0).upper()
    return None


def _check_statements(files: list[tuple[Path, str, str]]) -> None:
    for path, version, _digest in files:
        for statement in _statements(path.read_text(encoding="utf-8")):
            keyword = _forbidden_keyword(statement)
            if keyword is not None:
                raise MigrationError(
                    f"migration {version} contains {keyword}; the runner manages the transaction",
                    details={"version": version, "keyword": keyword},
                )


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


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    """Roll back after a failure without letting a failed ROLLBACK replace the original error."""

    if conn.in_transaction:
        with suppress(sqlite3.Error):  # closing the connection rolls the transaction back
            conn.execute("ROLLBACK")


def apply_migrations(conn: sqlite3.Connection, directory: Path) -> list[str]:
    """Apply ``*.sqlite.sql`` files. Unchanged re-runs are no-ops.

    A pre-existing ``schema_migrations`` without ``checksum`` is backfilled from
    the current file bytes so databases created by the old runner still open.

    Atomic across processes: what to apply is decided again after ``BEGIN
    IMMEDIATE`` takes the write lock, and every statement and its
    ``schema_migrations`` row commit together, so concurrent openers of the same
    file apply each migration once. A changed checksum raises
    :class:`MigrationChecksumError`; files before it stay applied, nothing at or
    after it is. A migration whose statement fails rolls back the whole run: no
    table or row from it, or from any file applied earlier in the same run, is
    kept, and the error is raised.

    A file whose statements include ``BEGIN``, ``COMMIT``, ``END``, ``ROLLBACK``,
    ``SAVEPOINT``, ``RELEASE``, ``VACUUM`` or ``PRAGMA journal_mode`` raises
    :class:`MigrationError` before anything is applied.

    ``conn`` must use legacy transaction control (the default) or
    ``isolation_level=None``; a Python 3.12 ``autocommit=False`` connection
    raises :class:`TypeError`.
    """

    if getattr(conn, "autocommit", None) is False:
        raise TypeError(
            "apply_migrations needs a legacy or isolation_level=None connection, not autocommit=False"
        )
    files = _migration_files(directory)
    _check_statements(files)
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
        _rollback_quietly(conn)
        raise
    if mismatch is not None:
        raise mismatch
    return newly
