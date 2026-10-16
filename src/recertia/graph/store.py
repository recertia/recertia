"""Per-node checkpointing of ``RunState`` (M0).

Checkpointing after every node is what makes a run resumable at node granularity: killing the
process mid-run and restarting it re-reads the last saved ``(node, next_node, state)`` row and
continues from there, rather than replaying the whole run from ``intake``.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from contracts.run import RunState
from recertia.persistence.db import open_db


class CheckpointConflict(RuntimeError):
    """A checkpoint ``(run_id, seq)`` was inserted twice. Sequences are write-once."""


class CheckpointStore:
    """SQLite-backed: one row per ``(run_id, seq)``, monotonically increasing per run."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = open_db(self._db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                node TEXT NOT NULL,
                next_node TEXT,
                state_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (run_id, seq)
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def save(self, run_id: str, seq: int, node: str, next_node: str | None, state: RunState) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO checkpoints (run_id, seq, node, next_node, state_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, datetime('now'))",
                    (run_id, seq, node, next_node, state.model_dump_json()),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise CheckpointConflict(
                    f"checkpoint ({run_id!r}, seq={seq}) already exists"
                ) from exc

    def latest(self, run_id: str) -> tuple[int, str, str | None, RunState] | None:
        """Returns ``(seq, node, next_node, state)`` for the most recent checkpoint, or ``None``."""

        with self._lock:
            row = self._conn.execute(
                "SELECT seq, node, next_node, state_json FROM checkpoints "
                "WHERE run_id=? ORDER BY seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        seq, node, next_node, state_json = row
        return seq, node, next_node, RunState.model_validate_json(state_json)

    def latest_seq(self, run_id: str) -> int | None:
        """Latest checkpoint ``seq`` for ``run_id`` without parsing the state payload."""

        with self._lock:
            row = self._conn.execute(
                "SELECT seq FROM checkpoints WHERE run_id=? ORDER BY seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return int(row[0]) if row is not None else None

    def history(self, run_id: str) -> list[tuple[int, str, str | None, RunState]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, node, next_node, state_json FROM checkpoints "
                "WHERE run_id=? ORDER BY seq ASC",
                (run_id,),
            ).fetchall()
        return [(seq, node, next_node, RunState.model_validate_json(sj)) for seq, node, next_node, sj in rows]

    def list_run_ids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT run_id FROM checkpoints ORDER BY run_id"
            ).fetchall()
        return [r[0] for r in rows]
