"""SQLite-backed migration program store (Goal packs)."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from contracts.program import MigrationProgram
from recertia.persistence.db import open_db


class ProgramStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = open_db(self.path)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS programs (
                    program_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, program_id)
                )
                """
            )

    def close(self) -> None:
        self._conn.close()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def put(self, program: MigrationProgram) -> MigrationProgram:
        if not program.program_id:
            program = program.model_copy(update={"program_id": uuid4().hex[:12]})
        now = self._now()
        if not program.created_at:
            program = program.model_copy(update={"created_at": now})
        program = program.model_copy(update={"updated_at": now})
        payload = program.model_dump(mode="json")
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO programs(program_id, tenant_id, status, payload, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, program_id) DO UPDATE SET
                    status=excluded.status,
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (
                    program.program_id,
                    program.tenant_id,
                    program.status,
                    json.dumps(payload),
                    program.updated_at,
                ),
            )
        return program

    def get(self, program_id: str, *, tenant_id: str) -> MigrationProgram | None:
        row = self._conn.execute(
            "SELECT payload FROM programs WHERE program_id = ? AND tenant_id = ?",
            (program_id, tenant_id),
        ).fetchone()
        if row is None:
            return None
        return MigrationProgram.model_validate(json.loads(row["payload"]))

    def list(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        limit: int = 100,
    ) -> list[MigrationProgram]:
        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if status:
            clauses.append("status = ?")
            params.append(status)
        params.append(max(1, min(limit, 100)))
        sql = (
            "SELECT payload FROM programs WHERE "
            + " AND ".join(clauses)
            + " ORDER BY updated_at DESC LIMIT ?"
        )
        rows = self._conn.execute(sql, params).fetchall()
        return [MigrationProgram.model_validate(json.loads(r["payload"])) for r in rows]
