"""Weekly job-quota reserve, settle, and release.

Spend lives in ``job_quota.sqlite`` beside the JSON sidecar. ``QuotaSidecar``
stays the operator export: settle writes spent columns back to ``job_quota.json``.
A crash after reserve leaves the reservation until settle, release, or the TTL.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from contracts.policy import JobQuota
from recertia.errors import QuotaExceeded
from recertia.persistence.db import apply_migrations, open_db
from recertia.policy_load import QuotaSidecar, iso_week_id
from recertia.ports.identity import tenant_id

_MIGRATIONS = Path(__file__).resolve().parents[1] / "persistence" / "migrations" / "quota"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(at: datetime | None = None) -> str:
    when = at or _now()
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.isoformat()


def _parse_stamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _loads_map(raw: str) -> dict[str, int]:
    data = json.loads(raw or "{}")
    if not isinstance(data, dict):
        raise QuotaExceeded("job quota class map is corrupt")
    return {str(key): int(value) for key, value in data.items()}


class JobQuotaRepository:
    """One connection to ``job_quota.sqlite`` for a single tenant."""

    def __init__(self, path: Path | str, *, tenant: str = "local") -> None:
        self.path = Path(path)
        self.tenant = str(tenant_id(tenant))
        self._lock = threading.Lock()
        self._conn = open_db(self.path)
        self._conn.isolation_level = None
        self._conn.row_factory = sqlite3.Row
        apply_migrations(self._conn, _MIGRATIONS)

    def close(self) -> None:
        self._conn.close()

    def import_sidecar(self, path: Path | str, base: JobQuota, *, at: datetime | None = None) -> None:
        """Copy this week's JSON spend into SQLite when the week row is absent."""

        sidecar_path = Path(path)
        if not sidecar_path.exists():
            return
        sidecar = QuotaSidecar(sidecar_path)
        raw = sidecar._read()
        week = iso_week_id(at)
        if not raw or raw.get("week_id") != week:
            return
        merged = sidecar.merge(base, at=at)
        with self._lock:
            self._begin()
            try:
                if self._week(week) is not None:
                    self._rollback()
                    return
                self._insert_week(
                    week,
                    tokens_spent=merged.tokens_spent,
                    hex_tokens_spent=merged.hex_tokens_spent,
                    computer_use_tokens_spent=merged.computer_use_tokens_spent,
                    hex_jobs=merged.hex_jobs_by_class,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._rollback()
                raise

    def seed_open_week(self, quota: JobQuota, *, at: datetime | None = None) -> None:
        """Record in-memory spend when neither SQLite nor the sidecar has this week."""

        if (
            quota.tokens_spent == 0
            and quota.hex_tokens_spent == 0
            and quota.computer_use_tokens_spent == 0
            and not quota.hex_jobs_by_class
        ):
            return
        week = iso_week_id(at)
        with self._lock:
            self._begin()
            try:
                if self._week(week) is not None:
                    self._rollback()
                    return
                self._insert_week(
                    week,
                    tokens_spent=quota.tokens_spent,
                    hex_tokens_spent=quota.hex_tokens_spent,
                    computer_use_tokens_spent=quota.computer_use_tokens_spent,
                    hex_jobs=quota.hex_jobs_by_class,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._rollback()
                raise

    def snapshot(self, base: JobQuota, *, at: datetime | None = None) -> JobQuota:
        """Fold this week's spent and reserved amounts into ``base`` for admission."""

        row = self._week(iso_week_id(at))
        if row is None:
            return base
        settled = _loads_map(str(row["hex_jobs_by_class_json"]))
        reserved = _loads_map(str(row["hex_jobs_reserved_json"]))
        classes = set(settled) | set(reserved)
        by_class = {key: settled.get(key, 0) + reserved.get(key, 0) for key in classes}
        return base.model_copy(
            update={
                "tokens_spent": int(row["tokens_spent"]) + int(row["tokens_reserved"]),
                "hex_tokens_spent": int(row["hex_tokens_spent"]) + int(row["hex_tokens_reserved"]),
                "computer_use_tokens_spent": int(row["computer_use_tokens_spent"])
                + int(row["computer_use_tokens_reserved"]),
                "hex_jobs_by_class": by_class,
            }
        )

    def reserve(
        self,
        quota: JobQuota,
        *,
        job: str,
        priority: str,
        tokens: int,
        task_class: str | None = None,
        at: datetime | None = None,
        created_at: datetime | None = None,
    ) -> str:
        """Hold ``tokens`` against the weekly caps. Raises ``QuotaExceeded``."""

        if tokens < 0:
            raise QuotaExceeded("job quota reserve requires a non-negative token count")
        week = iso_week_id(at)
        hex_tokens = tokens if priority == "practice_hex" else 0
        cu_tokens = (
            tokens
            if JobQuota._computer_use_class(task_class) and priority in ("practice_band", "practice_hex")
            else 0
        )
        class_slot = task_class if priority == "practice_hex" and task_class else None
        reservation_id = uuid.uuid4().hex
        stamp = _stamp(created_at or at)
        with self._lock:
            self._begin()
            try:
                self._ensure_week(week)
                if not self._hold(
                    quota,
                    week,
                    tokens=tokens,
                    hex_tokens=hex_tokens,
                    cu_tokens=cu_tokens,
                    class_slot=class_slot,
                ):
                    self._rollback()
                    raise QuotaExceeded("weekly job quota exceeded")
                self._conn.execute(
                    """
                    INSERT INTO job_quota_ledger (
                        tenant_id, reservation_id, week_id, job_name, priority,
                        tokens_reserved, computer_use_tokens, hex_tokens, task_class,
                        status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?)
                    """,
                    (
                        self.tenant,
                        reservation_id,
                        week,
                        job,
                        priority,
                        tokens,
                        cu_tokens,
                        hex_tokens,
                        class_slot,
                        stamp,
                    ),
                )
                self._conn.execute("COMMIT")
            except QuotaExceeded:
                raise
            except Exception:
                self._rollback()
                raise
        return reservation_id

    def settle(self, reservation_id: str) -> None:
        """Move a reservation onto spent. A second call is a no-op."""

        self._finish(reservation_id, settle=True)

    def release(self, reservation_id: str) -> None:
        """Drop a reservation without charging. A second call is a no-op."""

        self._finish(reservation_id, settle=False)

    def release_expired(self, *, ttl_s: int | None = None, now: datetime | None = None) -> int:
        """Release reservations older than ``ttl_s`` (default ``RECERTIA_QUOTA_RESERVE_TTL_S``)."""

        if ttl_s is None:
            from recertia.settings import load_settings

            ttl_s = load_settings().quota_reserve_ttl_s
        moment = now or _now()
        released = 0
        for reservation_id, created_at in self._reserved_created():
            age = (moment - _parse_stamp(created_at)).total_seconds()
            if age >= ttl_s:
                self.release(reservation_id)
                released += 1
        return released

    def reserved_ids(self) -> list[str]:
        return [reservation_id for reservation_id, _created in self._reserved_created()]

    def totals(self, *, at: datetime | None = None) -> tuple[int, int]:
        """Return ``(tokens_spent, tokens_reserved)`` for the current week."""

        row = self._week(iso_week_id(at))
        if row is None:
            return (0, 0)
        return (int(row["tokens_spent"]), int(row["tokens_reserved"]))

    def computer_use_spent(self, *, at: datetime | None = None) -> int:
        row = self._week(iso_week_id(at))
        if row is None:
            return 0
        return int(row["computer_use_tokens_spent"])

    def publish(self, path: Path | str, template: JobQuota, *, at: datetime | None = None) -> None:
        """Write settled spend to the JSON sidecar. Reserved amounts stay in SQLite."""

        row = self._week(iso_week_id(at))
        if row is None:
            return
        jobs = _loads_map(str(row["hex_jobs_by_class_json"]))
        spent = template.model_copy(
            update={
                "tokens_spent": int(row["tokens_spent"]),
                "hex_tokens_spent": int(row["hex_tokens_spent"]),
                "computer_use_tokens_spent": int(row["computer_use_tokens_spent"]),
                "hex_jobs_by_class": jobs,
            }
        )
        QuotaSidecar(path).save(spent, at=at)

    def _finish(self, reservation_id: str, *, settle: bool) -> None:
        with self._lock:
            self._begin()
            try:
                row = self._conn.execute(
                    """
                    SELECT week_id, tokens_reserved, computer_use_tokens, hex_tokens,
                           task_class, status
                    FROM job_quota_ledger
                    WHERE tenant_id = ? AND reservation_id = ?
                    """,
                    (self.tenant, reservation_id),
                ).fetchone()
                if row is None or str(row["status"]) != "reserved":
                    self._rollback()
                    return
                self._unhold(
                    str(row["week_id"]),
                    tokens=int(row["tokens_reserved"]),
                    hex_tokens=int(row["hex_tokens"]),
                    cu_tokens=int(row["computer_use_tokens"]),
                    class_slot=str(row["task_class"]) if row["task_class"] else None,
                    settle=settle,
                )
                status = "settled" if settle else "released"
                settled_tokens = int(row["tokens_reserved"]) if settle else None
                self._conn.execute(
                    """
                    UPDATE job_quota_ledger
                    SET status = ?, tokens_settled = ?, settled_at = ?
                    WHERE tenant_id = ? AND reservation_id = ? AND status = 'reserved'
                    """,
                    (status, settled_tokens, _stamp(), self.tenant, reservation_id),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._rollback()
                raise

    def _reserved_created(self) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            """
            SELECT reservation_id, created_at FROM job_quota_ledger
            WHERE tenant_id = ? AND status = 'reserved'
            """,
            (self.tenant,),
        ).fetchall()
        return [(str(row["reservation_id"]), str(row["created_at"])) for row in rows]

    def _hold(
        self,
        quota: JobQuota,
        week: str,
        *,
        tokens: int,
        hex_tokens: int,
        cu_tokens: int,
        class_slot: str | None,
    ) -> bool:
        row = self._week(week)
        if row is None:
            return False
        token_cap = quota.weekly_token_cap
        hex_cap = int(quota.weekly_token_cap * quota.hex_share)
        cu_cap = int(quota.weekly_token_cap * quota.computer_use_practice_share)
        if int(row["tokens_spent"]) + int(row["tokens_reserved"]) + tokens > token_cap:
            return False
        if int(row["hex_tokens_spent"]) + int(row["hex_tokens_reserved"]) + hex_tokens > hex_cap:
            return False
        if (
            int(row["computer_use_tokens_spent"]) + int(row["computer_use_tokens_reserved"]) + cu_tokens
            > cu_cap
        ):
            return False
        settled = _loads_map(str(row["hex_jobs_by_class_json"]))
        reserved = _loads_map(str(row["hex_jobs_reserved_json"]))
        if class_slot is not None:
            used = settled.get(class_slot, 0) + reserved.get(class_slot, 0)
            if used >= quota.max_hex_jobs_per_task_class:
                return False
            reserved[class_slot] = reserved.get(class_slot, 0) + 1
        self._conn.execute(
            """
            UPDATE job_quota_week
            SET tokens_reserved = tokens_reserved + ?,
                hex_tokens_reserved = hex_tokens_reserved + ?,
                computer_use_tokens_reserved = computer_use_tokens_reserved + ?,
                hex_jobs_reserved_json = ?
            WHERE tenant_id = ? AND week_id = ?
            """,
            (
                tokens,
                hex_tokens,
                cu_tokens,
                json.dumps(reserved),
                self.tenant,
                week,
            ),
        )
        return True

    def _unhold(
        self,
        week: str,
        *,
        tokens: int,
        hex_tokens: int,
        cu_tokens: int,
        class_slot: str | None,
        settle: bool,
    ) -> None:
        row = self._week(week)
        if row is None:
            return
        settled = _loads_map(str(row["hex_jobs_by_class_json"]))
        reserved = _loads_map(str(row["hex_jobs_reserved_json"]))
        if class_slot is not None:
            reserved[class_slot] = max(0, reserved.get(class_slot, 0) - 1)
            if reserved[class_slot] == 0:
                reserved.pop(class_slot, None)
            if settle:
                settled[class_slot] = settled.get(class_slot, 0) + 1
        if settle:
            self._conn.execute(
                """
                UPDATE job_quota_week
                SET tokens_reserved = MAX(0, tokens_reserved - ?),
                    tokens_spent = tokens_spent + ?,
                    hex_tokens_reserved = MAX(0, hex_tokens_reserved - ?),
                    hex_tokens_spent = hex_tokens_spent + ?,
                    computer_use_tokens_reserved = MAX(0, computer_use_tokens_reserved - ?),
                    computer_use_tokens_spent = computer_use_tokens_spent + ?,
                    hex_jobs_by_class_json = ?,
                    hex_jobs_reserved_json = ?
                WHERE tenant_id = ? AND week_id = ?
                """,
                (
                    tokens,
                    tokens,
                    hex_tokens,
                    hex_tokens,
                    cu_tokens,
                    cu_tokens,
                    json.dumps(settled),
                    json.dumps(reserved),
                    self.tenant,
                    week,
                ),
            )
            return
        self._conn.execute(
            """
            UPDATE job_quota_week
            SET tokens_reserved = MAX(0, tokens_reserved - ?),
                hex_tokens_reserved = MAX(0, hex_tokens_reserved - ?),
                computer_use_tokens_reserved = MAX(0, computer_use_tokens_reserved - ?),
                hex_jobs_reserved_json = ?
            WHERE tenant_id = ? AND week_id = ?
            """,
            (
                tokens,
                hex_tokens,
                cu_tokens,
                json.dumps(reserved),
                self.tenant,
                week,
            ),
        )

    def _ensure_week(self, week: str) -> None:
        if self._week(week) is None:
            self._insert_week(week)

    def _insert_week(
        self,
        week: str,
        *,
        tokens_spent: int = 0,
        hex_tokens_spent: int = 0,
        computer_use_tokens_spent: int = 0,
        hex_jobs: dict[str, int] | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO job_quota_week (
                tenant_id, week_id, tokens_spent, hex_tokens_spent,
                computer_use_tokens_spent, hex_jobs_by_class_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                self.tenant,
                week,
                tokens_spent,
                hex_tokens_spent,
                computer_use_tokens_spent,
                json.dumps(hex_jobs or {}),
            ),
        )

    def _week(self, week: str) -> sqlite3.Row | None:
        row = self._conn.execute(
            "SELECT * FROM job_quota_week WHERE tenant_id = ? AND week_id = ?",
            (self.tenant, week),
        ).fetchone()
        return row

    def _begin(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def _rollback(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            return
