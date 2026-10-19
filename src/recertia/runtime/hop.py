"""One-transaction hop commit (REL-5a / F16).

Checkpoint, trajectory events, route entries, and outbox rows commit together
so a crash cannot leave a taped event without its checkpoint.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from contracts.run import RouteEntry, RunState
from contracts.trajectory import TrajectoryEvent
from recertia.faults import fault_point
from recertia.persistence.fence import require_lease_token
from recertia.runtime.lease import LeaseToken

HOP_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS trajectory_events (
    tenant_id  TEXT NOT NULL,
    run_id     TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    hop_seq    INTEGER NOT NULL,
    event_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, run_id, seq)
);
CREATE TABLE IF NOT EXISTS trajectory_meta (
    tenant_id TEXT NOT NULL,
    run_id    TEXT NOT NULL,
    meta_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, run_id)
);
CREATE TABLE IF NOT EXISTS route_entries (
    tenant_id  TEXT NOT NULL,
    run_id     TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    entry_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, run_id, seq)
);
CREATE TABLE IF NOT EXISTS outbox (
    id            INTEGER PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    kind          TEXT NOT NULL,
    dedupe_key    TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('pending','delivered','dead')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    available_at  TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    delivered_at  TEXT,
    UNIQUE (tenant_id, kind, dedupe_key)
);
"""


@dataclass(frozen=True)
class OutboxRow:
    """One outbox enqueue. ``dedupe_key`` is unique per ``(tenant_id, kind)``."""

    kind: str
    dedupe_key: str
    payload_json: str


def ensure_hop_tables(conn: sqlite3.Connection) -> None:
    """Create hop tables on connections that skip the runtime migrator."""

    conn.executescript(HOP_TABLES_SQL)
    conn.commit()


def _begin_immediate(db: sqlite3.Connection) -> None:
    try:
        db.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        db.rollback()
        db.execute("BEGIN IMMEDIATE")


def insert_hop(
    db: sqlite3.Connection,
    *,
    tenant_id: str,
    run_id: str,
    seq: int,
    node: str,
    next_node: str | None,
    state: RunState,
    trajectory: Sequence[TrajectoryEvent],
    route_entry: RouteEntry | None,
    outbox_rows: Sequence[OutboxRow],
    fencing_token: int | None,
    import_events: Sequence[TrajectoryEvent] = (),
    meta_json: str | None = None,
    eval_outbox: Any | None = None,
) -> None:
    """Insert hop rows. The caller owns the surrounding transaction."""

    db.execute(
        "INSERT INTO checkpoints ("
        "run_id, seq, node, next_node, state_json, created_at, fencing_token"
        ") VALUES (?, ?, ?, ?, ?, datetime('now'), ?)",
        (run_id, seq, node, next_node, state.model_dump_json(), fencing_token),
    )
    terminal = str(state.terminal) if state.terminal else None
    db.execute(
        """
        INSERT INTO run_summaries (
            run_id, status, terminal, updated_at, cost_usd,
            task_class, attempt_no, arm, created_at
        ) VALUES (?, ?, ?, datetime('now'), ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(run_id) DO UPDATE SET
            status = excluded.status,
            terminal = excluded.terminal,
            updated_at = excluded.updated_at,
            cost_usd = excluded.cost_usd,
            task_class = excluded.task_class,
            attempt_no = excluded.attempt_no,
            arm = excluded.arm
        """,
        (
            run_id,
            terminal or "running",
            terminal,
            float(state.spent.cost_usd),
            state.task.task_class or "repo-chore",
            int(state.attempt_no),
            str(state.arm),
        ),
    )
    has_runs = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'"
    ).fetchone()
    if has_runs is not None:
        summary = {
            "run_id": run_id,
            "task_class": state.task.task_class or "repo-chore",
            "terminal": terminal,
            "status": terminal or "running",
            "attempt_no": int(state.attempt_no),
            "cost_usd": float(state.spent.cost_usd),
            "arm": str(state.arm),
            "tenant_id": tenant_id,
        }
        db.execute(
            """
            UPDATE runs SET summary_json = ?, status = ?, updated_at = datetime('now')
            WHERE tenant_id = ? AND run_id = ?
            """,
            (
                json.dumps(summary, sort_keys=True),
                terminal or "running",
                tenant_id,
                run_id,
            ),
        )
    if eval_outbox is not None:
        db.execute(
            """
            INSERT INTO eval_outbox (
                observation_key, tenant_id, run_id, attempt_no,
                payload_json, status, attempts, created_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', 0, datetime('now'))
            ON CONFLICT(observation_key) DO NOTHING
            """,
            (
                eval_outbox.observation_key,
                eval_outbox.tenant_id,
                eval_outbox.run_id,
                eval_outbox.attempt_no,
                eval_outbox.payload_json,
            ),
        )
    if meta_json is not None:
        db.execute(
            """
            INSERT INTO trajectory_meta (tenant_id, run_id, meta_json)
            VALUES (?, ?, ?)
            ON CONFLICT(tenant_id, run_id) DO NOTHING
            """,
            (tenant_id, run_id, meta_json),
        )
    existing = db.execute(
        "SELECT COUNT(*) FROM trajectory_events WHERE tenant_id=? AND run_id=?",
        (tenant_id, run_id),
    ).fetchone()
    if existing is not None and int(existing[0]) == 0:
        seen_seq: set[int] = set()
        for event in import_events:
            assigned = event.model_copy(update={"run_id": run_id})
            seq_no = int(assigned.seq)
            if seq_no in seen_seq:
                continue
            seen_seq.add(seq_no)
            db.execute(
                "INSERT INTO trajectory_events "
                "(tenant_id, run_id, seq, hop_seq, event_json) VALUES (?, ?, ?, ?, ?)",
                (tenant_id, run_id, seq_no, 0, assigned.model_dump_json()),
            )
    row = db.execute(
        "SELECT COALESCE(MAX(seq), -1) FROM trajectory_events WHERE tenant_id=? AND run_id=?",
        (tenant_id, run_id),
    ).fetchone()
    next_event_seq = int(row[0]) + 1 if row is not None else 0
    for event in trajectory:
        assigned = event.model_copy(update={"seq": next_event_seq, "run_id": run_id})
        db.execute(
            "INSERT INTO trajectory_events "
            "(tenant_id, run_id, seq, hop_seq, event_json) VALUES (?, ?, ?, ?, ?)",
            (tenant_id, run_id, next_event_seq, seq, assigned.model_dump_json()),
        )
        next_event_seq += 1
    if route_entry is not None:
        db.execute(
            "INSERT INTO route_entries (tenant_id, run_id, seq, entry_json) VALUES (?, ?, ?, ?)",
            (tenant_id, run_id, seq, route_entry.model_dump_json()),
        )
    now = datetime.now(timezone.utc).isoformat()
    for outbox in outbox_rows:
        db.execute(
            """
            INSERT INTO outbox (
                tenant_id, kind, dedupe_key, payload_json, status,
                attempts, available_at, created_at
            ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
            ON CONFLICT(tenant_id, kind, dedupe_key) DO NOTHING
            """,
            (tenant_id, outbox.kind, outbox.dedupe_key, outbox.payload_json, now, now),
        )


def commit_hop(
    db: sqlite3.Connection,
    token: LeaseToken | None,
    *,
    seq: int,
    node: str,
    next_node: str | None,
    state: RunState,
    trajectory: Sequence[TrajectoryEvent] = (),
    route_entry: RouteEntry | None = None,
    outbox_rows: Sequence[OutboxRow] = (),
    import_events: Sequence[TrajectoryEvent] = (),
    meta_json: str | None = None,
    eval_outbox: Any | None = None,
    lease_db: Path | str | None = None,
    tenant_id: str = "local",
    run_id: str | None = None,
    fencing_token: int | None = None,
    missing_row: Literal["reject", "allow"] = "reject",
) -> None:
    """One transaction. Raises ``LeaseLost`` or ``sqlite3.IntegrityError``."""

    tid = str(token.tenant_id) if token is not None else tenant_id
    rid = token.run_id if token is not None else (run_id if run_id is not None else state.run_id)
    fencing = token.fencing_token if token is not None else fencing_token
    _begin_immediate(db)
    try:
        if lease_db is not None:
            require_lease_token(
                Path(lease_db),
                tenant_id=tid,
                run_id=rid,
                fencing_token=fencing,
                conn=db,
                missing_row=missing_row,
            )
        fault_point("checkpoint.before_commit")
        insert_hop(
            db,
            tenant_id=tid,
            run_id=rid,
            seq=seq,
            node=node,
            next_node=next_node,
            state=state,
            trajectory=trajectory,
            route_entry=route_entry,
            outbox_rows=outbox_rows,
            fencing_token=fencing,
            import_events=import_events,
            meta_json=meta_json,
            eval_outbox=eval_outbox,
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    fault_point("checkpoint.after_commit")
