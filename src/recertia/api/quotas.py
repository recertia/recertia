"""Per-tenant quota accounting for the API surface (roadmap P2-3)."""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class QuotaExceeded(Exception):
    """Tenant would exceed a configured daily or in-flight quota."""


@dataclass(frozen=True)
class TenantQuota:
    max_runs_per_day: int = 100
    max_cost_usd_per_day: float = 50.0
    max_in_flight: int = 4


def quota_from_env() -> TenantQuota:
    return TenantQuota(
        max_runs_per_day=int(os.environ.get("RECERTIA_TENANT_MAX_RUNS_PER_DAY", "100")),
        max_cost_usd_per_day=float(os.environ.get("RECERTIA_TENANT_MAX_COST_USD_PER_DAY", "50")),
        max_in_flight=int(os.environ.get("RECERTIA_TENANT_MAX_IN_FLIGHT", "4")),
    )


class QuotaStore:
    """SQLite-backed per-tenant daily counters + in-flight gauge."""

    def __init__(self, path: Path | str, *, defaults: TenantQuota | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.defaults = defaults or quota_from_env()
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS daily (
                    tenant_id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    runs INTEGER NOT NULL DEFAULT 0,
                    cost_usd REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (tenant_id, day)
                );
                CREATE TABLE IF NOT EXISTS inflight (
                    tenant_id TEXT PRIMARY KEY,
                    count INTEGER NOT NULL DEFAULT 0
                );
                """
            )

    def close(self) -> None:
        self._conn.close()

    def reset_inflight(self) -> None:
        """Zero leaked in-flight gauges. Nothing is running at process start.

        A crash between ``admit`` and ``release``/``complete`` used to leave
        ``inflight.count`` high and 429 the tenant until the counter was edited
        by hand. Queued work does not survive this process, so the honest count
        at startup is zero.
        """

        with self._lock, self._conn:
            self._conn.execute("UPDATE inflight SET count = 0")

    def _day(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def admit(self, tenant_id: str, *, quota: TenantQuota | None = None) -> None:
        """Raise :class:`QuotaExceeded` when the tenant cannot start another run."""

        limits = quota or self.defaults
        day = self._day()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT runs, cost_usd FROM daily WHERE tenant_id = ? AND day = ?",
                (tenant_id, day),
            ).fetchone()
            runs = int(row["runs"]) if row else 0
            cost = float(row["cost_usd"]) if row else 0.0
            if runs >= limits.max_runs_per_day:
                raise QuotaExceeded(
                    f"tenant {tenant_id} exceeded max_runs_per_day={limits.max_runs_per_day}"
                )
            if cost >= limits.max_cost_usd_per_day:
                raise QuotaExceeded(
                    f"tenant {tenant_id} exceeded max_cost_usd_per_day={limits.max_cost_usd_per_day}"
                )
            inflight = self._conn.execute(
                "SELECT count FROM inflight WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
            current = int(inflight["count"]) if inflight else 0
            if current >= limits.max_in_flight:
                raise QuotaExceeded(
                    f"tenant {tenant_id} exceeded max_in_flight={limits.max_in_flight}"
                )
            self._conn.execute(
                """
                INSERT INTO inflight(tenant_id, count) VALUES (?, 1)
                ON CONFLICT(tenant_id) DO UPDATE SET count = count + 1
                """,
                (tenant_id,),
            )

    def release_inflight(self, tenant_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE inflight
                SET count = CASE WHEN count > 0 THEN count - 1 ELSE 0 END
                WHERE tenant_id = ?
                """,
                (tenant_id,),
            )

    def complete(self, tenant_id: str, *, cost_usd: float = 0.0) -> None:
        day = self._day()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO daily(tenant_id, day, runs, cost_usd) VALUES (?, ?, 1, ?)
                ON CONFLICT(tenant_id, day) DO UPDATE SET
                    runs = runs + 1,
                    cost_usd = cost_usd + excluded.cost_usd
                """,
                (tenant_id, day, float(cost_usd)),
            )
            self._conn.execute(
                """
                UPDATE inflight
                SET count = CASE WHEN count > 0 THEN count - 1 ELSE 0 END
                WHERE tenant_id = ?
                """,
                (tenant_id,),
            )

    def snapshot(self, tenant_id: str) -> dict[str, float | int | str]:
        day = self._day()
        row = self._conn.execute(
            "SELECT runs, cost_usd FROM daily WHERE tenant_id = ? AND day = ?",
            (tenant_id, day),
        ).fetchone()
        inflight = self._conn.execute(
            "SELECT count FROM inflight WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
        return {
            "tenant_id": tenant_id,
            "day": day,
            "runs": int(row["runs"]) if row else 0,
            "cost_usd": float(row["cost_usd"]) if row else 0.0,
            "in_flight": int(inflight["count"]) if inflight else 0,
            "max_runs_per_day": self.defaults.max_runs_per_day,
            "max_cost_usd_per_day": self.defaults.max_cost_usd_per_day,
            "max_in_flight": self.defaults.max_in_flight,
        }
