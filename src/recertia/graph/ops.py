"""Stable operation ids and at-least-once execution (M0; refactor-plan B6).

Every side-effecting call a node makes — a tool invocation, a ledger append — is keyed by
``(run_id, incarnation, attempt_no, node, op_seq)``. Before performing the side effect, the caller checks
whether that key already has a recorded result; if so, it returns the stored result instead
of repeating the effect. This is what makes "kill the process mid-run and resume" safe: a
resumed run re-enters a node from its last checkpoint and may re-call the same operations, but
each one either no-ops (already applied) or runs exactly once (not yet applied) — never twice.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Literal, TypeVar

from recertia.persistence.db import open_db
from recertia.persistence.root_lock import RootBusy, RootLock

T = TypeVar("T")


class OperationConflict(RuntimeError):
    """A duplicate ``put`` of the same operation key is refused (write-once)."""

_PENDING_SENTINEL: dict[str, bool] = {"__pending__": True}
_PENDING_POLL_S = 0.01
_PENDING_TIMEOUT_S = 5.0

OpKey = tuple[str, int, int, str, int]


def _is_pending(result: object) -> bool:
    return isinstance(result, dict) and result == _PENDING_SENTINEL


class _LiveClaim:
    """In-process holder of a pending operation row."""

    __slots__ = ("done", "owner")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.owner = threading.current_thread()


class OperationLedger:
    """SQLite-backed idempotency store, one row per applied operation."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        pending_timeout_s: float = _PENDING_TIMEOUT_S,
        pending_poll_s: float = _PENDING_POLL_S,
        lease_db: Path | str | None = None,
        tenant_id: str = "local",
        schema_lock: bool = True,
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lease_db = Path(lease_db) if lease_db is not None else self._db_path
        self._tenant_id = tenant_id
        self._lock = threading.Lock()
        self._pending_timeout_s = pending_timeout_s
        self._pending_poll_s = pending_poll_s
        self._live_claims: dict[OpKey, _LiveClaim] = {}
        self._conn = open_db(self._db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        if schema_lock:
            _init_operations_table(self._conn, self._db_path)
        else:
            ensure_operations_schema(self._conn)

    def close(self) -> None:
        self._conn.close()

    # ... (other methods unchanged for this edit)

_OPERATIONS_DDL = """
CREATE TABLE operations (
    run_id TEXT NOT NULL,
    incarnation INTEGER NOT NULL DEFAULT 1,
    attempt_no INTEGER NOT NULL,
    node TEXT NOT NULL,
    op_seq INTEGER NOT NULL,
    result_json TEXT NOT NULL,
    fencing_token INTEGER,
    PRIMARY KEY (run_id, incarnation, attempt_no, node, op_seq)
)
"""


def _init_operations_table(conn: sqlite3.Connection, db_path: Path) -> None:
    """Create or rebuild ``operations`` so incarnation is in the primary key.

    The rebuild is exclusive under the runtime root lock and runs in one SQLite
    transaction. Bounded wait: RECERTIA_IMPORT_LOCK_TIMEOUT_S (default 5s).
    Raises typed RootBusy if the lock cannot be taken; nothing is written.
    """

    if _operations_schema_ready(conn):
        return
    timeout_s = float(os.environ.get("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "5"))
    lock = RootLock(Path(db_path).parent / ".lock")
    try:
        lock.acquire_exclusive(timeout_s=timeout_s)
    except Exception as exc:
        raise RootBusy(f"operations schema lock held: {lock.path}") from exc
    try:
        if _operations_schema_ready(conn):
            return
        _rebuild_operations_table(conn)
    finally:
        lock.release()


def ensure_operations_schema(conn: sqlite3.Connection) -> None:
    if not _operations_schema_ready(conn):
        _rebuild_operations_table(conn)


def _operations_schema_ready(conn: sqlite3.Connection) -> bool:
    names = _table_names(conn)
    if "operations_rel3" in names:
        return False
    if "operations" not in names:
        return False
    return _operations_pk(conn) == ["run_id", "incarnation", "attempt_no", "node", "op_seq"]


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _operations_pk(conn: sqlite3.Connection) -> list[str]:
    info = list(conn.execute("PRAGMA table_info(operations)"))
    return [str(row[1]) for row in sorted((r for r in info if r[5]), key=lambda r: r[5])]


def _rebuild_operations_table(conn: sqlite3.Connection) -> None:
    previous = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            names = _table_names(conn)
            if "operations_rel3" in names and "operations" not in names:
                conn.execute("ALTER TABLE operations_rel3 RENAME TO operations")
                names = _table_names(conn)
            elif "operations_rel3" in names:
                conn.execute("DROP TABLE operations_rel3")
                names = _table_names(conn)
            if "operations" not in names:
                conn.execute(_OPERATIONS_DDL)
                conn.execute("COMMIT")
                return
            if _operations_pk(conn) == ["run_id", "incarnation", "attempt_no", "node", "op_seq"]:
                conn.execute("COMMIT")
                return
            info = list(conn.execute("PRAGMA table_info(operations)"))
            cols = {str(row[1]) for row in info}
            incarnation = "incarnation" if "incarnation" in cols else "1"
            fencing = "fencing_token" if "fencing_token" in cols else "NULL"
            conn.execute(_OPERATIONS_DDL.replace("operations", "operations_rel3", 1))
            conn.execute(
                f"""
                INSERT INTO operations_rel3 (
                    run_id, incarnation, attempt_no, node, op_seq, result_json, fencing_token
                )
                SELECT run_id, {incarnation}, attempt_no, node, op_seq, result_json, {fencing}
                FROM operations
                """
            )
            conn.execute("DROP TABLE operations")
            conn.execute("ALTER TABLE operations_rel3 RENAME TO operations")
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    finally:
        conn.isolation_level = previous
