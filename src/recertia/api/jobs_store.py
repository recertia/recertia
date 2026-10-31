"""Durable job run records for console C1."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from recertia.persistence.db import open_db


@dataclass
class JobRunRecord:
    job_run_id: str
    job: str
    tenant_id: str
    status: str = "queued"  # queued|running|succeeded|failed
    created_at: str = ""
    finished_at: str | None = None
    dry_run: bool = False
    proposals: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public_dict(self) -> dict[str, Any]:
        """Run-scope view (``GET /v1/jobs``): promote jobs never show refusal detail.

        REC-009: new promote records are decision-only at the source. Records written
        before that (no ``decision_only`` marker) may hold fixture ids in ``error`` and
        ``meta.failing_fixtures``; both are redacted here.
        """

        out = self.to_dict()
        if self.job != "promote":
            return out
        meta = dict(out.get("meta") or {})
        meta.pop("failing_fixtures", None)
        if not meta.get("decision_only") and out.get("error"):
            out["error"] = "promotion refused (detail redacted)"
        out["meta"] = meta
        return out


class JobRunStore:
    """API-process-only store (#159): it takes no root lock and is covered by the API
    shared ``api.lock``.

    Every thread shares one connection, so ``get`` and ``list`` hold ``_lock`` like the
    writes do: concurrent ``execute`` calls of the same SQL text share one cached prepared
    statement and reset each other's (REC-114).
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = open_db(self.path)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS job_runs (
                    job_run_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    job TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def close(self) -> None:
        self._conn.close()

    def create(
        self,
        job: str,
        *,
        tenant_id: str,
        dry_run: bool = False,
        meta: dict | None = None,
    ) -> JobRunRecord:
        rec = JobRunRecord(
            job_run_id=uuid4().hex[:12],
            job=job,
            tenant_id=tenant_id,
            status="queued",
            created_at=datetime.now(timezone.utc).isoformat(),
            dry_run=dry_run,
            meta=meta or {},
        )
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO job_runs(
                    job_run_id, tenant_id, job, status, payload, created_at
                ) VALUES (?,?,?,?,?,?)
                """,
                (
                    rec.job_run_id,
                    tenant_id,
                    job,
                    rec.status,
                    json.dumps(rec.to_dict()),
                    rec.created_at,
                ),
            )
        return rec

    def save(self, rec: JobRunRecord) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE job_runs SET status = ?, payload = ? WHERE job_run_id = ? AND tenant_id = ?",
                (rec.status, json.dumps(rec.to_dict()), rec.job_run_id, rec.tenant_id),
            )

    def get(self, job_run_id: str, *, tenant_id: str) -> JobRunRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM job_runs WHERE job_run_id = ? AND tenant_id = ?",
                (job_run_id, tenant_id),
            ).fetchone()
        if row is None:
            return None
        return JobRunRecord(**json.loads(row["payload"]))

    def list(self, *, tenant_id: str, limit: int = 50) -> list[JobRunRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM job_runs WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ?",
                (tenant_id, max(1, min(limit, 100))),
            ).fetchall()
        return [JobRunRecord(**json.loads(r["payload"])) for r in rows]
