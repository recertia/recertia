"""SQLite ``run_queue`` with deficit-round-robin claim.

Create and resume enqueue here. ``RECERTIA_API_DEFAULT_ASYNC`` stays off, so
omitted ``mode`` enqueue-and-waits. In-flight is a query of claimed/running
rows, not a counter that can leak across a crash.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from recertia.errors import QuotaExceeded
from recertia.persistence.db import apply_migrations, open_db_with_init
from recertia.persistence.layout import RuntimeLayout
from recertia.ports.identity import tenant_id

_MIGRATIONS = Path(__file__).resolve().parents[1] / "persistence" / "migrations" / "control"
_CLAIM_COST = 1
_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "dead"})
# A claim refused with RootBusy (backup or restore holds the runtime root lock, or an
# import or open handle of the same tenant holds its tenant lock; REC-065) goes back
# to 'queued' this long (REC-058).
ROOT_BUSY_RETRY_S = 2.0
QueueStatus = Literal["succeeded", "failed", "cancelled"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class QueueItem:
    tenant_id: str
    run_id: str
    kind: str
    payload: dict[str, Any]
    priority: int = 0
    max_attempts: int = 3
    enqueued_at: str | None = None
    not_before: str | None = None


@dataclass(frozen=True)
class Claim:
    tenant_id: str
    run_id: str
    kind: str
    claim_token: int
    attempt: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class QueueRow:
    tenant_id: str
    run_id: str
    kind: str
    status: str
    claim_token: int
    attempt: int
    payload: dict[str, Any]
    error_code: str | None


class RunQueue:
    """One connection to ``root/control.db``."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._conn = open_db_with_init(self.path, self._set_up)

    def _set_up(self, conn: sqlite3.Connection) -> None:
        """Post-connect steps; ``open_db_with_init`` closes ``conn`` if one raises."""

        conn.isolation_level = None
        conn.row_factory = sqlite3.Row
        apply_migrations(conn, _MIGRATIONS)
        _ensure_cancel_column(conn)
        from recertia.settings import load_settings

        settings = load_settings()
        self._deficit_cap = settings.sched_deficit_cap
        self._default_max_attempts = settings.queue_max_attempts
        self._default_max_queue_depth = settings.tenant_max_queue_depth

    def close(self) -> None:
        self._conn.close()

    def enqueue(self, item: QueueItem, *, replace_terminal: bool = False) -> None:
        tenant = str(tenant_id(item.tenant_id))
        if item.kind not in {"run", "eval", "job"}:
            raise ValueError(f"unsupported queue kind {item.kind!r}")
        stamp = item.enqueued_at or _now()
        due = item.not_before or stamp
        attempts = item.max_attempts or self._default_max_attempts
        payload = json.dumps(item.payload)
        with self._lock:
            self._ensure_tenant(tenant)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                depth_row = self._conn.execute(
                    "SELECT COUNT(*) FROM run_queue WHERE tenant_id=? AND status='queued'",
                    (tenant,),
                ).fetchone()
                depth = int(depth_row[0]) if depth_row else 0
                cap_row = self._conn.execute(
                    "SELECT max_queue_depth FROM tenant_limits WHERE tenant_id=?",
                    (tenant,),
                ).fetchone()
                cap = int(cap_row[0]) if cap_row else self._default_max_queue_depth
                existing = self._conn.execute(
                    "SELECT status FROM run_queue WHERE tenant_id=? AND run_id=? AND kind=?",
                    (tenant, item.run_id, item.kind),
                ).fetchone()
                if existing is None:
                    if depth >= cap:
                        self._conn.execute("ROLLBACK")
                        raise QuotaExceeded(
                            f"tenant {tenant} exceeded max_queue_depth={cap}"
                        )
                    self._conn.execute(
                        """
                        INSERT INTO run_queue (
                            tenant_id, run_id, kind, status, priority, enqueued_at, not_before,
                            claim_token, attempt, max_attempts, payload_json, cancel_requested
                        ) VALUES (?, ?, ?, 'queued', ?, ?, ?, 0, 0, ?, ?, 0)
                        """,
                        (
                            tenant,
                            item.run_id,
                            item.kind,
                            item.priority,
                            stamp,
                            due,
                            attempts,
                            payload,
                        ),
                    )
                elif replace_terminal and str(existing["status"]) in _TERMINAL:
                    if depth >= cap:
                        self._conn.execute("ROLLBACK")
                        raise QuotaExceeded(
                            f"tenant {tenant} exceeded max_queue_depth={cap}"
                        )
                    self._conn.execute(
                        """
                        UPDATE run_queue
                        SET status='queued', priority=?, enqueued_at=?, not_before=?,
                            claimed_by=NULL, heartbeat_at=NULL, attempt=0,
                            max_attempts=?, payload_json=?, error_code=NULL,
                            finished_at=NULL, cancel_requested=0
                        WHERE tenant_id=? AND run_id=? AND kind=?
                        """,
                        (
                            item.priority,
                            stamp,
                            due,
                            attempts,
                            payload,
                            tenant,
                            item.run_id,
                            item.kind,
                        ),
                    )
                else:
                    self._conn.execute("ROLLBACK")
                    raise ValueError(
                        f"queue item {(tenant, item.run_id, item.kind)} already exists"
                    )
                self._conn.execute("COMMIT")
            except QuotaExceeded:
                raise
            except ValueError:
                raise
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def claim(self, worker_id: str, *, limit: int = 1) -> list[Claim]:
        claimed: list[Claim] = []
        for _ in range(max(0, limit)):
            one = self._claim_one(worker_id)
            if one is None:
                break
            claimed.append(one)
        return claimed

    def heartbeat_claim(self, claim: Claim) -> None:
        stamp = _now()
        with self._lock:
            self._conn.execute(
                """
                UPDATE run_queue SET heartbeat_at=?
                WHERE tenant_id=? AND run_id=? AND kind=? AND claim_token=?
                  AND status IN ('claimed', 'running')
                """,
                (stamp, claim.tenant_id, claim.run_id, claim.kind, claim.claim_token),
            )

    def mark_running(self, claim: Claim) -> None:
        self._transition(claim, status="running", error_code=None, from_status=("claimed",))

    def finish(
        self,
        claim: Claim,
        *,
        status: QueueStatus,
        error_code: str | None = None,
    ) -> None:
        self._transition(
            claim,
            status=status,
            error_code=error_code,
            from_status=("claimed", "running"),
        )

    def requeue(self, claim: Claim, *, delay_s: float, error_code: str) -> None:
        """Return a claimed/running item to ``queued`` without spending an attempt.

        For a refusal that says nothing about the run itself (``RootBusy``): the
        item is claimable again after ``delay_s``. ``error_code`` records why.
        """

        due = (datetime.now(timezone.utc) + timedelta(seconds=max(0.0, delay_s))).isoformat()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE run_queue
                SET status='queued', claimed_by=NULL, heartbeat_at=NULL, not_before=?,
                    error_code=?
                WHERE tenant_id=? AND run_id=? AND kind=? AND claim_token=?
                  AND status IN ('claimed', 'running')
                """,
                (due, error_code, claim.tenant_id, claim.run_id, claim.kind, claim.claim_token),
            )
        if cur.rowcount != 1:
            raise RuntimeError(
                f"stale claim {(claim.tenant_id, claim.run_id, claim.kind)} token={claim.claim_token}"
            )

    def wait(
        self,
        tenant: str,
        run_id: str,
        *,
        timeout_s: float,
        kind: str = "run",
        poll_s: float = 0.05,
    ) -> QueueRow | None:
        deadline = time.monotonic() + timeout_s
        while True:
            row = self.get(tenant, run_id, kind=kind)
            if row is not None and row.status in _TERMINAL:
                return row
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll_s)

    def get(self, tenant: str, run_id: str, *, kind: str = "run") -> QueueRow | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT tenant_id, run_id, kind, status, claim_token, attempt,
                       payload_json, error_code
                FROM run_queue WHERE tenant_id=? AND run_id=? AND kind=?
                """,
                (str(tenant_id(tenant)), run_id, kind),
            ).fetchone()
        if row is None:
            return None
        return _row(row)

    def inflight(self, tenant: str) -> int:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) FROM run_queue
                WHERE tenant_id=? AND kind='run' AND status IN ('claimed', 'running')
                """,
                (str(tenant_id(tenant)),),
            ).fetchone()
        return int(row[0]) if row else 0

    def depth(self, tenant: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM run_queue WHERE tenant_id=? AND status='queued'",
                (str(tenant_id(tenant)),),
            ).fetchone()
        return int(row[0]) if row else 0

    def set_max_in_flight(self, tenant: str, max_in_flight: int) -> None:
        ident = str(tenant_id(tenant))
        with self._lock:
            self._ensure_tenant(ident)
            self._conn.execute(
                "UPDATE tenant_limits SET max_in_flight=? WHERE tenant_id=?",
                (max_in_flight, ident),
            )

    def set_maintenance(self, mode: str) -> None:
        if mode not in {"open", "quiescing", "frozen"}:
            raise ValueError(f"unsupported maintenance mode {mode!r}")
        with self._lock:
            self._conn.execute(
                "UPDATE maintenance SET mode=?, since=? WHERE id=1",
                (mode, _now()),
            )

    def max_in_flight(self, tenant: str) -> int:
        ident = str(tenant_id(tenant))
        with self._lock:
            self._ensure_tenant(ident)
            row = self._conn.execute(
                "SELECT max_in_flight FROM tenant_limits WHERE tenant_id=?",
                (ident,),
            ).fetchone()
        from recertia.settings import load_settings

        return int(row[0]) if row else load_settings().tenant_max_in_flight

    def max_queue_depth(self, tenant: str) -> int:
        ident = str(tenant_id(tenant))
        with self._lock:
            self._ensure_tenant(ident)
            row = self._conn.execute(
                "SELECT max_queue_depth FROM tenant_limits WHERE tenant_id=?",
                (ident,),
            ).fetchone()
        return int(row[0]) if row else self._default_max_queue_depth

    def heartbeat_worker(self, worker_id: str) -> None:
        stamp = _now()
        with self._lock:
            self._conn.execute(
                "UPDATE workers SET heartbeat_at=? WHERE worker_id=?",
                (stamp, worker_id),
            )
            inflight = self._conn.execute(
                "SELECT COUNT(*) FROM run_queue WHERE claimed_by=? AND status IN ('claimed', 'running')",
                (worker_id,),
            ).fetchone()
            self._conn.execute(
                "UPDATE workers SET inflight=? WHERE worker_id=?",
                (int(inflight[0]) if inflight else 0, worker_id),
            )

    def request_cancel(
        self, tenant: str, run_id: str, *, kind: str = "run", by: str | None = None
    ) -> None:
        del by
        ident = str(tenant_id(tenant))
        with self._lock:
            self._conn.execute(
                """
                UPDATE run_queue SET cancel_requested=1
                WHERE tenant_id=? AND run_id=? AND kind=?
                  AND status IN ('queued', 'claimed', 'running')
                """,
                (ident, run_id, kind),
            )

    def cancel_requested(self, tenant: str, run_id: str, *, kind: str = "run") -> bool:
        ident = str(tenant_id(tenant))
        with self._lock:
            row = self._conn.execute(
                """
                SELECT cancel_requested FROM run_queue
                WHERE tenant_id=? AND run_id=? AND kind=?
                """,
                (ident, run_id, kind),
            ).fetchone()
        if row is None or row[0] is None:
            return False
        return bool(row[0])

    def outstanding(self) -> int:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) FROM run_queue
                WHERE status IN ('queued', 'claimed', 'running')
                """
            ).fetchone()
        return int(row[0]) if row else 0

    def reconcile_orphans(self) -> int:
        """Requeue or fail claims whose worker process is gone.

        A live pid keeps its claim. A missing worker row, or a pid that does not
        exist, is an expired lease: one more attempt goes back to ``queued``,
        and an exhausted attempt becomes ``dead``.
        """

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT q.tenant_id, q.run_id, q.kind, q.attempt, q.max_attempts, w.pid
                FROM run_queue q
                LEFT JOIN workers w ON w.worker_id = q.claimed_by
                WHERE q.status IN ('claimed', 'running')
                """
            ).fetchall()
            changed = 0
            stamp = _now()
            for row in rows:
                if _pid_alive(row["pid"]):
                    continue
                attempt = int(row["attempt"])
                max_attempts = int(row["max_attempts"])
                key = (row["tenant_id"], row["run_id"], row["kind"])
                if attempt + 1 >= max_attempts:
                    self._conn.execute(
                        """
                        UPDATE run_queue
                        SET status='dead', error_code='lease_expired', finished_at=?
                        WHERE tenant_id=? AND run_id=? AND kind=?
                          AND status IN ('claimed', 'running')
                        """,
                        (stamp, *key),
                    )
                else:
                    delay = min(2 ** (attempt + 1), 60)
                    due = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
                    self._conn.execute(
                        """
                        UPDATE run_queue
                        SET status='queued', attempt=?, claimed_by=NULL, not_before=?,
                            heartbeat_at=NULL
                        WHERE tenant_id=? AND run_id=? AND kind=?
                          AND status IN ('claimed', 'running')
                        """,
                        (attempt + 1, due, *key),
                    )
                changed += 1
            return changed

    def register_worker(self, worker_id: str, *, capacity: int, hostname: str, pid: int) -> None:
        stamp = _now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO workers (
                    worker_id, started_at, heartbeat_at, hostname, pid, capacity, inflight
                ) VALUES (?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(worker_id) DO UPDATE SET
                    heartbeat_at=excluded.heartbeat_at,
                    hostname=excluded.hostname,
                    pid=excluded.pid,
                    capacity=excluded.capacity
                """,
                (worker_id, stamp, stamp, hostname, pid, capacity),
            )

    def _claim_one(self, worker_id: str) -> Claim | None:
        stamp = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                mode = self._conn.execute("SELECT mode FROM maintenance WHERE id=1").fetchone()
                if mode is None or mode[0] in {"frozen", "quiescing"}:
                    self._conn.execute("COMMIT")
                    return None
                picked = self._conn.execute(
                    """
                    SELECT q.tenant_id, q.run_id, q.kind, q.attempt, q.payload_json, q.claim_token
                    FROM run_queue q
                    JOIN tenant_sched s ON s.tenant_id = q.tenant_id
                    JOIN tenant_limits lim ON lim.tenant_id = q.tenant_id
                    WHERE q.status = 'queued'
                      AND q.not_before <= ?
                      AND (
                        SELECT COUNT(*) FROM run_queue r
                        WHERE r.tenant_id = q.tenant_id
                          AND r.status IN ('claimed', 'running')
                      ) < lim.max_in_flight
                    ORDER BY s.deficit DESC, q.enqueued_at ASC
                    LIMIT 1
                    """,
                    (stamp,),
                ).fetchone()
                if picked is None:
                    self._conn.execute("COMMIT")
                    return None
                tenant = str(picked["tenant_id"])
                run_id = str(picked["run_id"])
                kind = str(picked["kind"])
                attempt = int(picked["attempt"])
                payload = _object_dict(picked["payload_json"])
                token = int(picked["claim_token"])
                cur = self._conn.execute(
                    """
                    UPDATE run_queue
                    SET status='claimed', claimed_by=?, claim_token=claim_token + 1,
                        heartbeat_at=?
                    WHERE tenant_id=? AND run_id=? AND kind=? AND status='queued'
                    """,
                    (worker_id, stamp, tenant, run_id, kind),
                )
                if cur.rowcount != 1:
                    self._conn.execute("ROLLBACK")
                    return None
                self._conn.execute(
                    """
                    UPDATE tenant_sched
                    SET deficit = MIN(deficit + weight, ?)
                    WHERE tenant_id != ?
                    """,
                    (self._deficit_cap, tenant),
                )
                self._conn.execute(
                    """
                    UPDATE tenant_sched
                    SET deficit = deficit - ?, last_claim_at=?
                    WHERE tenant_id=?
                    """,
                    (_CLAIM_COST, stamp, tenant),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return Claim(
            tenant_id=tenant,
            run_id=run_id,
            kind=kind,
            claim_token=token + 1,
            attempt=attempt,
            payload=payload,
        )

    def _transition(
        self,
        claim: Claim,
        *,
        status: str,
        error_code: str | None,
        from_status: tuple[str, ...],
    ) -> None:
        placeholders = ", ".join("?" for _ in from_status)
        stamp = _now()
        finished = stamp if status in _TERMINAL else None
        with self._lock:
            cur = self._conn.execute(
                f"""
                UPDATE run_queue
                SET status=?, heartbeat_at=?, error_code=?, finished_at=COALESCE(?, finished_at)
                WHERE tenant_id=? AND run_id=? AND kind=? AND claim_token=?
                  AND status IN ({placeholders})
                """,
                (
                    status,
                    stamp,
                    error_code,
                    finished,
                    claim.tenant_id,
                    claim.run_id,
                    claim.kind,
                    claim.claim_token,
                    *from_status,
                ),
            )
        if cur.rowcount == 1:
            return
        current = self.get(claim.tenant_id, claim.run_id, kind=claim.kind)
        if current is not None and current.status == status and current.claim_token == claim.claim_token:
            return
        raise RuntimeError(
            f"stale claim {(claim.tenant_id, claim.run_id, claim.kind)} token={claim.claim_token}"
        )

    def _ensure_tenant(self, tenant: str) -> None:
        from recertia.settings import load_settings

        settings = load_settings()
        self._conn.execute(
            """
            INSERT INTO tenant_sched (tenant_id, deficit, weight)
            VALUES (?, 0, 1)
            ON CONFLICT(tenant_id) DO NOTHING
            """,
            (tenant,),
        )
        self._conn.execute(
            """
            INSERT INTO tenant_limits (
                tenant_id, max_runs_per_day, max_cost_usd_per_day, max_in_flight,
                max_queue_depth, max_sse_connections, max_model_spend_usd_per_day,
                weekly_token_cap
            ) VALUES (?, ?, ?, ?, ?, 32, ?, NULL)
            ON CONFLICT(tenant_id) DO NOTHING
            """,
            (
                tenant,
                settings.tenant_max_runs_per_day,
                settings.tenant_max_cost_usd_per_day,
                settings.tenant_max_in_flight,
                settings.tenant_max_queue_depth,
                settings.tenant_max_cost_usd_per_day,
            ),
        )


def open_run_queue(root: Path | str) -> RunQueue:
    """Open the run queue (REC-066 documented exception).

    The API process is covered by the shared ``api.lock`` before stores are
    assembled. Workers and CLI open ``control.db`` without a root lock so the
    claim path can still raise typed ``RootBusy`` (and requeue) when a backup
    holds the root lock (REC-058). The control db is small; the window is the
    claim duration.
    """
    return RunQueue(RuntimeLayout(Path(root)).control_db)


def _ensure_cancel_column(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(run_queue)")}
    if "cancel_requested" not in cols:
        conn.execute(
            "ALTER TABLE run_queue ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"
        )


def _pid_alive(pid: object) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int | str):
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return True


def _object_dict(raw: object) -> dict[str, Any]:
    parsed = json.loads(raw if isinstance(raw, str) else "{}")
    if isinstance(parsed, dict):
        return parsed
    return {}


def _row(row: sqlite3.Row) -> QueueRow:
    return QueueRow(
        tenant_id=row["tenant_id"],
        run_id=row["run_id"],
        kind=row["kind"],
        status=row["status"],
        claim_token=int(row["claim_token"]),
        attempt=int(row["attempt"]),
        payload=_object_dict(row["payload_json"]),
        error_code=row["error_code"],
    )
