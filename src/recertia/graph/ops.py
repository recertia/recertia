"""Stable operation ids and at-least-once execution (M0; refactor-plan B6).

Every side-effecting call a node makes — a tool invocation, a ledger append — is keyed by
``(run_id, attempt_no, node, op_seq)``. Before performing the side effect, the caller checks
whether that key already has a recorded result; if so, it returns the stored result instead
of repeating the effect. This is what makes "kill the process mid-run and resume" safe: a
resumed run re-enters a node from its last checkpoint and may re-call the same operations, but
each one either no-ops (already applied) or runs exactly once (not yet applied) — never twice.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable, TypeVar

from recertia.persistence.db import open_db

T = TypeVar("T")

_PENDING_SENTINEL: dict[str, bool] = {"__pending__": True}
_PENDING_POLL_S = 0.01
_PENDING_TIMEOUT_S = 5.0


def _is_pending(result: object) -> bool:
    return isinstance(result, dict) and result == _PENDING_SENTINEL


class OperationLedger:
    """SQLite-backed idempotency store, one row per applied operation."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        pending_timeout_s: float = _PENDING_TIMEOUT_S,
        pending_poll_s: float = _PENDING_POLL_S,
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._pending_timeout_s = pending_timeout_s
        self._pending_poll_s = pending_poll_s
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

    def run_once(self, run_id: str, attempt_no: int, node: str, op_seq: int, fn: Callable[[], T]) -> T:
        """Execute ``fn`` only if ``(run_id, attempt_no, node, op_seq)`` has not already run.

        ``fn``'s return value MUST be JSON-serialisable — it is the durable record that lets a
        resumed run skip re-executing the side effect.

        Concurrent waiters block/retry while a claim is ``__pending__``. A crash-left pending
        row is never treated as success: after ``pending_timeout_s`` the claim is reclaimed and
        ``fn`` re-runs (at-least-once). Callers that observe a durable pending via ``get`` see
        it as not applied.
        """

        deadline = time.monotonic() + self._pending_timeout_s
        while True:
            claimed = False
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    row = self._conn.execute(
                        "SELECT result_json FROM operations "
                        "WHERE run_id=? AND attempt_no=? AND node=? AND op_seq=?",
                        (run_id, attempt_no, node, op_seq),
                    ).fetchone()
                    if row is not None:
                        stored = json.loads(row[0])
                        if not _is_pending(stored):
                            self._conn.execute("COMMIT")
                            return stored  # type: ignore[no-any-return]
                        if time.monotonic() >= deadline:
                            # Stale pending after crash / timed-out holder: reclaim.
                            self._conn.execute(
                                "UPDATE operations SET result_json=? "
                                "WHERE run_id=? AND attempt_no=? AND node=? AND op_seq=?",
                                (
                                    json.dumps(_PENDING_SENTINEL),
                                    run_id,
                                    attempt_no,
                                    node,
                                    op_seq,
                                ),
                            )
                            self._conn.execute("COMMIT")
                            claimed = True
                        else:
                            self._conn.execute("COMMIT")
                    else:
                        self._conn.execute(
                            "INSERT INTO operations (run_id, attempt_no, node, op_seq, result_json) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (
                                run_id,
                                attempt_no,
                                node,
                                op_seq,
                                json.dumps(_PENDING_SENTINEL),
                            ),
                        )
                        self._conn.execute("COMMIT")
                        claimed = True
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise

            if claimed:
                break
            time.sleep(self._pending_poll_s)

        try:
            result = fn()
        except Exception:
            with self._lock:
                self._conn.execute(
                    "DELETE FROM operations "
                    "WHERE run_id=? AND attempt_no=? AND node=? AND op_seq=? AND result_json=?",
                    (
                        run_id,
                        attempt_no,
                        node,
                        op_seq,
                        json.dumps(_PENDING_SENTINEL),
                    ),
                )
                self._conn.commit()
            raise

        if _is_pending(result):
            with self._lock:
                self._conn.execute(
                    "DELETE FROM operations "
                    "WHERE run_id=? AND attempt_no=? AND node=? AND op_seq=? AND result_json=?",
                    (
                        run_id,
                        attempt_no,
                        node,
                        op_seq,
                        json.dumps(_PENDING_SENTINEL),
                    ),
                )
                self._conn.commit()
            raise ValueError("operation fn must not return the pending sentinel")

        with self._lock:
            self._conn.execute(
                "UPDATE operations SET result_json=? WHERE run_id=? AND attempt_no=? AND node=? AND op_seq=?",
                (json.dumps(result), run_id, attempt_no, node, op_seq),
            )
            self._conn.commit()
        return result

    def count_for_node(self, run_id: str, attempt_no: int, node: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM operations WHERE run_id=? AND attempt_no=? AND node=? "
                "AND result_json != ?",
                (run_id, attempt_no, node, json.dumps(_PENDING_SENTINEL)),
            ).fetchone()
        return int(row[0]) if row else 0
