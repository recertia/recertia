"""Durable ``run_id`` registry in ``runtime.db`` (REL-3 F9)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recertia.persistence.db import open_db
from recertia.ports.identity import TenantId
from recertia.runtime.errors import RunIdExists

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
