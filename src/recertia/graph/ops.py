"""Stable operation ids and at-least-once execution (M0; refactor-plan B6).

Every side-effecting call a node makes — a tool invocation, a ledger append — is keyed by
``(run_id, attempt_no, node, op_seq)``. Before performing the side effect, the caller checks
whether that key already has a recorded result; if so, it returns the stored result instead
of repeating the effect. This is what makes "kill the process mid-run and resume" safe: a
resumed run re-enters a node from its last checkpoint and may re-call the same operations, but
each one either no-ops (already applied) or runs exactly once (not yet applied) — never twice.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, TypeVar

from recertia.graph.store import _ensure_column
from recertia.persistence.db import open_db

T = TypeVar("T")

_PENDING_SENTINEL: dict[str, bool] = {"__pending__": True}
_PENDING_POLL_S = 0.01


def _is_pending(result: object) -> bool:
    return isinstance(result, dict) and result == _PENDING_SENTINEL


class OperationLedger:
    """SQLite-backed idempotency store, one row per applied operation."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        pending_timeout_s: float = 0.0,
        pending_poll_s: float = _PENDING_POLL_S,
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # Accepted for callers that used to pass the 5s steal window. A live
        # owner's pending row is never reclaimed on a timer.
        self._pending_timeout_s = pending_timeout_s
        self._pending_poll_s = pending_poll_s
        self._inflight: dict[tuple[str, int, str, int], threading.Event] = {}
        self._oplock_dir = self._db_path.with_name(self._db_path.name + ".oplocks")
        self._conn = open_db(self._db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS operations (
                run_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                node TEXT NOT NULL,
                op_seq INTEGER NOT NULL,
                result_json TEXT NOT NULL,
                PRIMARY KEY (run_id, attempt_no, node, op_seq)
            )
            """
        )
        self._conn.commit()
        _ensure_column(self._conn, "operations", "fencing_token", "INTEGER")

    def close(self) -> None:
        self._conn.close()

    def get(self, run_id: str, attempt_no: int, node: str, op_seq: int) -> tuple[bool, object]:
        """Returns ``(already_applied, result)``. ``result`` is ``None`` when not applied.

        A crash-left ``__pending__`` sentinel is treated as not applied so resume can retry.
        """

        with self._lock:
            row = self._conn.execute(
                "SELECT result_json FROM operations WHERE run_id=? AND attempt_no=? AND node=? AND op_seq=?",
                (run_id, attempt_no, node, op_seq),
            ).fetchone()
        if row is None:
            return False, None
        result = json.loads(row[0])
        if _is_pending(result):
            return False, None
        return True, result

    def put(self, run_id: str, attempt_no: int, node: str, op_seq: int, result: object) -> None:
        if _is_pending(result):
            raise ValueError("refusing to persist pending sentinel as a durable result")
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO operations (run_id, attempt_no, node, op_seq, result_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, attempt_no, node, op_seq, json.dumps(result)),
            )
            self._conn.commit()

    def run_once(
        self,
        run_id: str,
        attempt_no: int,
        node: str,
        op_seq: int,
        fn: Callable[[], T],
        *,
        fencing_token: int | None = None,
    ) -> T:
        """Execute ``fn`` only if ``(run_id, attempt_no, node, op_seq)`` has not already run.

        ``fn``'s return value MUST be JSON-serialisable — it is the durable record that lets a
        resumed run skip re-executing the side effect.

        Concurrent waiters block while a claim is ``__pending__`` and its owner still holds
        the operation flock. There is no timer steal. A pending row whose flock is free is a
        dead owner and is reclaimed immediately. Callers that observe a durable pending via
        ``get`` see it as not applied.
        """

        key = (run_id, attempt_no, node, op_seq)
        oplock: int | None = None
        while oplock is None:
            with self._lock:
                waiting = self._inflight.get(key)
            if waiting is not None:
                waiting.wait(timeout=self._pending_poll_s)
                continue
            fd = self._try_oplock(key)
            if fd is None:
                time.sleep(self._pending_poll_s)
                continue
            with self._lock:
                if key in self._inflight:
                    _unlock_fd(fd)
                    continue
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    row = self._conn.execute(
                        "SELECT result_json FROM operations "
                        "WHERE run_id=? AND attempt_no=? AND node=? AND op_seq=?",
                        key,
                    ).fetchone()
                    if row is not None:
                        stored = json.loads(row[0])
                        if not _is_pending(stored):
                            self._conn.execute("COMMIT")
                            _unlock_fd(fd)
                            return stored  # type: ignore[no-any-return]
                    else:
                        self._conn.execute(
                            "INSERT INTO operations (run_id, attempt_no, node, op_seq, result_json) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (*key, json.dumps(_PENDING_SENTINEL)),
                        )
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    _unlock_fd(fd)
                    raise
                self._inflight[key] = threading.Event()
                oplock = fd

        try:
            try:
                result = fn()
            except Exception:
                self._delete_pending(key)
                raise
            if _is_pending(result):
                self._delete_pending(key)
                raise ValueError("operation fn must not return the pending sentinel")
            with self._lock:
                self._conn.execute(
                    "UPDATE operations SET result_json=?, fencing_token=? "
                    "WHERE run_id=? AND attempt_no=? AND node=? AND op_seq=?",
                    (json.dumps(result), fencing_token, *key),
                )
                self._conn.commit()
            return result
        finally:
            self._finish_claim(key, oplock)

    def _delete_pending(self, key: tuple[str, int, str, int]) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM operations "
                "WHERE run_id=? AND attempt_no=? AND node=? AND op_seq=? AND result_json=?",
                (*key, json.dumps(_PENDING_SENTINEL)),
            )
            self._conn.commit()

    def _try_oplock(self, key: tuple[str, int, str, int]) -> int | None:
        """Non-blocking exclusive flock. ``None`` means a live owner still holds it."""

        digest = hashlib.sha256(json.dumps(key).encode("utf-8")).hexdigest()
        path = self._oplock_dir / f"{digest}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        if _flock_nb(fd):
            return fd
        os.close(fd)
        return None

    def _finish_claim(self, key: tuple[str, int, str, int], oplock: int) -> None:
        with self._lock:
            done = self._inflight.pop(key, None)
        _unlock_fd(oplock)
        if done is not None:
            done.set()

    def count_for_node(self, run_id: str, attempt_no: int, node: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM operations WHERE run_id=? AND attempt_no=? AND node=? "
                "AND result_json != ?",
                (run_id, attempt_no, node, json.dumps(_PENDING_SENTINEL)),
            ).fetchone()
        return int(row[0]) if row else 0


def _flock_nb(fd: int) -> bool:
    if sys.platform == "win32":
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES}:
            return False
        raise
    return True


def _unlock_fd(fd: int) -> None:
    try:
        if sys.platform != "win32":
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
