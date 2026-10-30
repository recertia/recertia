"""Durable Idempotency-Key results for program step runs, in ``control.db`` (REC-056e).

A keyed bind is written in three steps, because the key lives in ``control.db`` and the
program in ``programs.sqlite`` and no transaction spans both files:

1. :meth:`IdempotencyStore.reserve` inserts a ``pending`` row (and sweeps expired rows);
2. the caller binds the step and saves the program;
3. :meth:`IdempotencyStore.complete` marks the row ``complete`` with the bound run_id.

If step 2 fails the caller calls :meth:`IdempotencyStore.release`, which deletes the
pending row. If the process dies between 2 and 3 the row stays ``pending``; a retry
with the same body takes it over (:attr:`Outcome.TAKEN_OVER`) once it is older than
:data:`LEASE_SECONDS`. A pending row never answers as a replay.

Every write is one ``BEGIN IMMEDIATE`` transaction. The store opens no connection until
its first write, so building it creates no file and no directory.
"""

from __future__ import annotations

import enum
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from recertia.persistence.db import apply_migrations, open_db
from recertia.settings import idempotency_ttl_seconds

__all__ = [
    "LEASE_SECONDS",
    "SWEEP_LIMIT",
    "IdempotencyStore",
    "KeyScope",
    "Outcome",
    "Reservation",
]

_MIGRATIONS = Path(__file__).resolve().parent / "migrations" / "control"

# A pending row younger than this belongs to a request that may still be running.
LEASE_SECONDS = 30
# At most this many expired rows are deleted by one write.
SWEEP_LIMIT = 500


@dataclass(frozen=True)
class KeyScope:
    """The primary key of a row. Each part is its own column, so no separator can collide."""

    tenant_id: str
    program_id: str
    step_id: str
    key: str


class Outcome(enum.Enum):
    RESERVED = "reserved"  # this request holds the pending row and must bind, then complete
    REPLAY = "replay"  # a complete row with the same request hash
    TAKEN_OVER = "taken_over"  # RESERVED, from a pending row whose lease ran out
    IN_PROGRESS = "in_progress"  # a pending row with the same hash, inside its lease
    MISMATCH = "mismatch"  # a live row with a different request hash


@dataclass(frozen=True)
class Reservation:
    outcome: Outcome
    run_id: str | None = None


def _now() -> int:
    return int(time.time())


class IdempotencyStore:
    """One writer connection to ``control.db``, opened on first write."""

    def __init__(
        self,
        path: Path | str,
        *,
        clock: Callable[[], int] = _now,
    ) -> None:
        self.path = Path(path)
        # RECERTIA_IDEMPOTENCY_TTL_SECONDS; a bad value raises ConfigError here, at startup.
        self.ttl_seconds = idempotency_ttl_seconds()
        self._clock = clock
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Close the writer. Safe to call more than once."""

        with self._lock:
            self._closed = True
            conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()

    def _writer(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("IdempotencyStore is closed")
        if self._conn is None:
            conn = open_db(self.path)
            try:
                apply_migrations(conn, _MIGRATIONS)
            except BaseException:
                conn.close()
                raise
            self._conn = conn
        return self._conn

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._writer()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
            conn.execute("COMMIT")

    @staticmethod
    def _where(scope: KeyScope) -> tuple[str, tuple[str, str, str, str]]:
        return (
            "tenant_id=? AND program_id=? AND step_id=? AND idempotency_key=?",
            (scope.tenant_id, scope.program_id, scope.step_id, scope.key),
        )

    def reserve(self, scope: KeyScope, request_hash: str) -> Reservation:
        """Step 1: claim ``scope`` for this request, or say why it cannot be claimed."""

        now = self._clock()
        where, args = self._where(scope)
        with self._txn() as conn:
            conn.execute(
                "DELETE FROM idempotency_keys WHERE rowid IN "
                "(SELECT rowid FROM idempotency_keys WHERE expires_at <= ? LIMIT ?)",
                (now, SWEEP_LIMIT),
            )
            # An expired row the bounded sweep did not reach is never honoured.
            conn.execute(f"DELETE FROM idempotency_keys WHERE {where} AND expires_at <= ?", (*args, now))
            try:
                conn.execute(
                    "INSERT INTO idempotency_keys (tenant_id, program_id, step_id, idempotency_key,"
                    " request_hash, state, run_id, created_at, expires_at)"
                    " VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?, ?)",
                    (*args, request_hash, now, now + self.ttl_seconds),
                )
                return Reservation(Outcome.RESERVED)
            except sqlite3.IntegrityError as exc:
                if getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY:
                    raise
            row = conn.execute(
                f"SELECT request_hash, state, run_id, created_at FROM idempotency_keys WHERE {where}",
                args,
            ).fetchone()
            stored_hash, state, run_id, created_at = row
            if stored_hash != request_hash:
                return Reservation(Outcome.MISMATCH)
            if state == "complete":
                return Reservation(Outcome.REPLAY, run_id)
            if now - int(created_at) < LEASE_SECONDS:
                return Reservation(Outcome.IN_PROGRESS)
            # The pending row outlived its lease: its request died between bind and complete.
            conn.execute(
                f"UPDATE idempotency_keys SET created_at=?, expires_at=? WHERE {where} AND state='pending'",
                (now, now + self.ttl_seconds, *args),
            )
            return Reservation(Outcome.TAKEN_OVER)

    def complete(self, scope: KeyScope, request_hash: str, run_id: str) -> bool:
        """Step 3: mark this request's pending row complete. ``False`` if it is not there."""

        where, args = self._where(scope)
        with self._txn() as conn:
            cur = conn.execute(
                f"UPDATE idempotency_keys SET state='complete', run_id=?"
                f" WHERE {where} AND state='pending' AND request_hash=?",
                (run_id, *args, request_hash),
            )
            return cur.rowcount == 1

    def release(self, scope: KeyScope, request_hash: str) -> None:
        """After a failed step 2: delete this request's pending row, so it claims nothing."""

        where, args = self._where(scope)
        with self._txn() as conn:
            conn.execute(
                f"DELETE FROM idempotency_keys WHERE {where} AND state='pending' AND request_hash=?",
                (*args, request_hash),
            )
