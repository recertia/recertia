"""Durable ``run_id`` registry in ``runtime.db`` (REL-3 F9)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recertia.persistence.db import open_db
from recertia.ports.identity import TenantId
from recertia.runtime.errors import RunIdExists

_LIST_INDEX = "CREATE INDEX IF NOT EXISTS runs_list ON runs (tenant_id, created_at, run_id)"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    request_hash TEXT NOT NULL DEFAULT '',
    summary_json TEXT NOT NULL DEFAULT '{}',
    workdir TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, run_id)
)
"""


@dataclass(frozen=True)
class ReserveResult:
    tenant_id: str
    run_id: str
    status: str
    request_hash: str
    created: bool = True


class RunRegistry:
    """Insert-only reservation of ``(tenant_id, run_id)`` in ``runtime.db``."""

    def __init__(self, db_path: Path | str) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = open_db(self.path)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute(_SCHEMA)
            self._conn.execute(_LIST_INDEX)

    def close(self) -> None:
        self._conn.close()

    def reserve(
        self,
        tenant: TenantId | str,
        run_id: str,
        *,
        request_hash: str,
        summary_json: str | dict[str, Any] = "{}",
        workdir: str | Path | None = None,
    ) -> ReserveResult:
        """INSERT a run row. Raises :class:`RunIdExists` on primary-key conflict."""

        tenant_id = str(tenant)
        if isinstance(summary_json, dict):
            summary_text = json.dumps(summary_json, sort_keys=True)
        else:
            summary_text = summary_json
        workdir_text = str(workdir) if workdir is not None else None
        now = datetime.now(timezone.utc).isoformat()
        status = "accepted"
        try:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO runs(
                        tenant_id, run_id, status, request_hash,
                        summary_json, workdir, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tenant_id,
                        run_id,
                        status,
                        request_hash,
                        summary_text,
                        workdir_text,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise RunIdExists(
                f"run_id already exists: {run_id}",
                run_id=run_id,
                details={"tenant_id": tenant_id},
            ) from exc
        return ReserveResult(
            tenant_id=tenant_id,
            run_id=run_id,
            status=status,
            request_hash=request_hash,
            created=True,
        )

    def get(self, tenant: TenantId | str, run_id: str) -> sqlite3.Row | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE tenant_id = ? AND run_id = ?",
            (str(tenant), run_id),
        ).fetchone()
        return row

    def list_page(
        self,
        tenant: TenantId | str,
        *,
        limit: int,
        cursor: Mapping[str, Any] | None = None,
        task_class: str | None = None,
        terminal: str | None = None,
    ) -> list[dict[str, Any]]:
        """Keyset page of ``summary_json``. Does not parse checkpoint state."""

        if limit < 1:
            return []
        tenant_id = str(tenant)
        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if cursor is not None:
            clauses.append("(created_at, run_id) < (?, ?)")
            params.extend([str(cursor["created_at"]), str(cursor["run_id"])])
        if task_class:
            clauses.append("json_extract(summary_json, '$.task_class') = ?")
            params.append(task_class)
        if terminal:
            clauses.append("json_extract(summary_json, '$.terminal') = ?")
            params.append(terminal)
        params.append(limit)
        sql = (
            "SELECT run_id, created_at, status, summary_json FROM runs WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC, run_id DESC LIMIT ?"
        )
        rows = self._conn.execute(sql, params).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            raw = row["summary_json"] or "{}"
            try:
                summary = json.loads(raw)
            except json.JSONDecodeError:
                summary = {}
            if not isinstance(summary, dict):
                summary = {}
            created = str(row["created_at"])
            run_id = str(row["run_id"])
            items.append(
                {
                    "run_id": run_id,
                    "task_class": summary.get("task_class") or "repo-chore",
                    "terminal": summary.get("terminal"),
                    "status": summary.get("status") or row["status"] or "running",
                    "attempt_no": summary.get("attempt_no"),
                    "created_at": summary.get("created_at") or created,
                    "cost_usd": summary.get("cost_usd"),
                    "arm": summary.get("arm"),
                    "tenant_id": summary.get("tenant_id") or tenant_id,
                }
            )
        return items
