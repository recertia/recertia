"""Per-node checkpointing of ``RunState`` (M0).

Checkpointing after every node is what makes a run resumable at node granularity: killing the
process mid-run and restarting it re-reads the last saved ``(node, next_node, state)`` row and
continues from there, rather than replaying the whole run from ``intake``.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from contracts.run import RouteEntry, RunState
from contracts.trajectory import TrajectoryEvent
from recertia.persistence.db import open_db
from recertia.runtime.hop import OutboxRow, commit_hop, ensure_hop_tables


class CheckpointConflict(RuntimeError):
    """A checkpoint ``(run_id, seq)`` was inserted twice. Sequences are write-once."""


@dataclass(frozen=True)
class EvalOutboxWrite:
    """One eval observation to enqueue in the same transaction as its checkpoint."""

    observation_key: str
    tenant_id: str
    run_id: str
    attempt_no: int
    payload_json: str


@dataclass(frozen=True)
class EvalOutboxPending:
    observation_key: str
    payload_json: str
    attempts: int


class CheckpointStore:
    """SQLite-backed: one row per ``(run_id, seq)``, monotonically increasing per run."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        lease_db: Path | str | None = None,
        tenant_id: str = "local",
    ) -> None:
        self._db_path = Path(db_path)
        self._lease_db = Path(lease_db) if lease_db is not None else None
        self._tenant_id = tenant_id
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
        _ensure_column(self._conn, "checkpoints", "fencing_token", "INTEGER")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS eval_outbox (
                observation_key TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS run_summaries (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                terminal TEXT,
                updated_at TEXT NOT NULL,
                cost_usd REAL,
                task_class TEXT NOT NULL,
                attempt_no INTEGER,
                arm TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()
        ensure_hop_tables(self._conn)

    def close(self) -> None:
        self._conn.close()

    def save(
        self,
        run_id: str,
        seq: int,
        node: str,
        next_node: str | None,
        state: RunState,
        *,
        fencing_token: int | None = None,
        eval_outbox: EvalOutboxWrite | None = None,
        trajectory: Sequence[TrajectoryEvent] = (),
        route_entry: RouteEntry | None = None,
        outbox_rows: Sequence[OutboxRow] = (),
        import_events: Sequence[TrajectoryEvent] = (),
        meta_json: str | None = None,
    ) -> None:
        with self._lock:
            lease_db = self._lease_db
            fenced = lease_db is not None
            missing_row: Literal["reject", "allow"] = (
                "allow" if fencing_token is None else "reject"
            )
            if fenced:
                from recertia.graph.lease import (
                    attach_lease_schema,
                    lease_paths_equal,
                    require_lease_token,
                )

                assert lease_db is not None
                require_lease_token(
                    lease_db,
                    tenant_id=self._tenant_id,
                    run_id=run_id,
                    fencing_token=fencing_token,
                    missing_row=missing_row,
                )
                if lease_db.exists() and not lease_paths_equal(lease_db, self._db_path):
                    attach_lease_schema(self._conn, lease_db)
            try:
                commit_hop(
                    self._conn,
                    None,
                    seq=seq,
                    node=node,
                    next_node=next_node,
                    state=state,
                    trajectory=trajectory,
                    route_entry=route_entry,
                    outbox_rows=outbox_rows,
                    import_events=import_events,
                    meta_json=meta_json,
                    eval_outbox=eval_outbox,
                    lease_db=lease_db if fenced else None,
                    tenant_id=self._tenant_id,
                    run_id=run_id,
                    fencing_token=fencing_token,
                    missing_row=missing_row,
                )
            except sqlite3.IntegrityError as exc:
                raise CheckpointConflict(
                    f"checkpoint ({run_id!r}, seq={seq}) already exists"
                ) from exc

    def _backfill_summaries(self) -> None:
        """Fill summaries for checkpoints written before the summary table existed."""

        self._conn.execute(
            """
            INSERT INTO run_summaries (
                run_id, status, terminal, updated_at, cost_usd,
                task_class, attempt_no, arm, created_at
            )
            SELECT
                c.run_id,
                CASE
                    WHEN json_extract(c.state_json, '$.terminal') IS NULL
                      OR json_extract(c.state_json, '$.terminal') = ''
                    THEN 'running'
                    ELSE json_extract(c.state_json, '$.terminal')
                END,
                NULLIF(json_extract(c.state_json, '$.terminal'), ''),
                c.created_at,
                json_extract(c.state_json, '$.spent.cost_usd'),
                COALESCE(json_extract(c.state_json, '$.task.task_class'), 'repo-chore'),
                json_extract(c.state_json, '$.attempt_no'),
                json_extract(c.state_json, '$.arm'),
                f.created_at
            FROM checkpoints c
            JOIN (
                SELECT run_id, MAX(seq) AS seq FROM checkpoints GROUP BY run_id
            ) l ON c.run_id = l.run_id AND c.seq = l.seq
            JOIN (
                SELECT run_id, MIN(created_at) AS created_at FROM checkpoints GROUP BY run_id
            ) f ON f.run_id = c.run_id
            WHERE c.run_id NOT IN (SELECT run_id FROM run_summaries)
            """
        )
        self._conn.commit()

    def list_trajectory_events(self, run_id: str) -> list[TrajectoryEvent]:
        """Events committed with hops, ordered by ``seq``."""

        with self._lock:
            rows = self._conn.execute(
                "SELECT event_json FROM trajectory_events "
                "WHERE tenant_id=? AND run_id=? ORDER BY seq ASC",
                (self._tenant_id, run_id),
            ).fetchall()
        return [TrajectoryEvent.model_validate_json(row[0]) for row in rows]

    def pending_eval_outbox(self) -> list[EvalOutboxPending]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT observation_key, payload_json, attempts
                FROM eval_outbox
                WHERE status = 'pending'
                ORDER BY created_at, observation_key
                """
            ).fetchall()
        return [
            EvalOutboxPending(str(key), str(payload), int(attempts))
            for key, payload, attempts in rows
        ]

    def mark_eval_outbox(self, observation_key: str, status: str, *, attempts: int) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE eval_outbox
                SET status = ?, attempts = ?
                WHERE observation_key = ?
                """,
                (status, attempts, observation_key),
            )
            self._conn.commit()

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
        return (
            int(row[0]),
            str(row[1]),
            None if row[2] is None else str(row[2]),
            RunState.model_validate_json(row[3]),
        )

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
        return [
            (
                int(seq),
                str(node),
                None if next_node is None else str(next_node),
                RunState.model_validate_json(sj),
            )
            for seq, node, next_node, sj in rows
        ]

    def list_run_ids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT run_id FROM checkpoints ORDER BY run_id"
            ).fetchall()
        return [str(r[0]) for r in rows]

    def list_summaries(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        task_class: str | None = None,
        terminal: str | None = None,
    ) -> list[dict[str, object]]:
        """Rows from ``run_summaries``. Does not parse checkpoint JSON on the read path.

        Checkpoints saved before the summary table are filled once with ``json_extract``.
        Returns at most ``limit`` rows ordered by ``run_id``.
        """

        if limit < 1:
            return []
        with self._lock:
            self._backfill_summaries()
            rows = self._conn.execute(
                """
                SELECT run_id, task_class, terminal, attempt_no, arm, cost_usd, created_at
                FROM run_summaries
                WHERE (? IS NULL OR run_id > ?)
                  AND (? IS NULL OR task_class = ?)
                  AND (? IS NULL OR IFNULL(terminal, '') = ?)
                ORDER BY run_id
                LIMIT ?
                """,
                (cursor, cursor, task_class, task_class, terminal, terminal, limit),
            ).fetchall()
        summaries: list[dict[str, object]] = []
        for run_id, task, terminal_value, attempt_no, arm, cost, created_at in rows:
            terminal_text = str(terminal_value) if terminal_value not in (None, "") else None
            summaries.append(
                {
                    "run_id": str(run_id),
                    "task_class": str(task or "repo-chore"),
                    "terminal": terminal_text,
                    "status": terminal_text or "running",
                    "attempt_no": int(attempt_no) if attempt_no is not None else None,
                    "created_at": created_at,
                    "cost_usd": float(cost) if cost is not None else None,
                    "arm": str(arm) if arm is not None else None,
                }
            )
        return summaries


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        conn.commit()
