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

from recertia.graph.store import _ensure_column
from recertia.persistence.db import open_db

T = TypeVar("T")

_PENDING_SENTINEL: dict[str, bool] = {"__pending__": True}
_PENDING_POLL_S = 0.01
_PENDING_TIMEOUT_S = 5.0

OpKey = tuple[str, int, str, int]


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
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # Kept for constructor compatibility. Live in-process owners are never
        # reclaimed by this timer (F7); abandoned pending rows are reclaimed immediately.
        self._pending_timeout_s = pending_timeout_s
        self._pending_poll_s = pending_poll_s
        self._live_claims: dict[OpKey, _LiveClaim] = {}
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

        Concurrent waiters block while a claim is ``__pending__`` and this process still has a
        live holder. There is no timer reclaim of a live in-process owner (F7). A crash-left
        pending row with no live holder is reclaimed immediately so resume can retry
        (at-least-once). Callers that observe a durable pending via ``get`` see it as not
        applied.
        """

        key: OpKey = (run_id, attempt_no, node, op_seq)
        wait_deadline = time.monotonic() + max(30.0, float(self._pending_timeout_s) * 6.0)
        while True:
            claimed = False
            wait_for: threading.Event | None = None
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
                        live = self._live_wait_event(key)
                        if live is not None:
                            wait_for = live
                            self._conn.execute("COMMIT")
                        else:
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
                            self._install_claim(key)
                            claimed = True
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
                        self._install_claim(key)
                        claimed = True
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise

            if claimed:
                break
            if time.monotonic() >= wait_deadline:
                # Safety valve: a waiter must not spin forever if a live claim
                # record is stale. Drop the claim and reclaim on the next pass.
                with self._lock:
                    stale = self._live_claims.pop(key, None)
                    if stale is not None:
                        stale.done.set()
                wait_deadline = time.monotonic() + max(30.0, float(self._pending_timeout_s) * 6.0)
                continue
            if wait_for is not None:
                wait_for.wait(timeout=self._pending_poll_s)
            else:
                time.sleep(self._pending_poll_s)

        try:
            result = fn()
            if _is_pending(result):
                raise ValueError("operation fn must not return the pending sentinel")
            with self._lock:
                self._conn.execute(
                    "UPDATE operations SET result_json=?, fencing_token=? "
                    "WHERE run_id=? AND attempt_no=? AND node=? AND op_seq=?",
                    (json.dumps(result), fencing_token, run_id, attempt_no, node, op_seq),
                )
                self._conn.commit()
                self._release_claim(key)
            return result
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
                self._release_claim(key)
            raise

    def count_for_node(self, run_id: str, attempt_no: int, node: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM operations WHERE run_id=? AND attempt_no=? AND node=? "
                "AND result_json != ?",
                (run_id, attempt_no, node, json.dumps(_PENDING_SENTINEL)),
            ).fetchone()
        return int(row[0]) if row else 0

    def _live_wait_event(self, key: OpKey) -> threading.Event | None:
        """Return the wait event for a live in-process holder, or ``None`` to reclaim."""

        claim = self._live_claims.get(key)
        if claim is None:
            return None
        if claim.done.is_set() or not claim.owner.is_alive():
            self._live_claims.pop(key, None)
            claim.done.set()
            return None
        return claim.done

    def _install_claim(self, key: OpKey) -> None:
        stale = self._live_claims.pop(key, None)
        if stale is not None:
            stale.done.set()
        self._live_claims[key] = _LiveClaim()

    def _release_claim(self, key: OpKey) -> None:
        claim = self._live_claims.pop(key, None)
        if claim is not None:
            claim.done.set()
