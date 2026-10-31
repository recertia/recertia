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
from recertia.runtime.errors import PreconditionFailed


class ProgramStore:
    """Programs in one SQLite file, on one connection that every thread shares.

    ``get``, ``list`` and ``put`` each hold ``_lock``, reads included. The connection's
    statement cache hands the same prepared statement to every ``execute`` of the same SQL
    text, so two threads stepping it at once reset each other's statement: a read could get
    no row for a program that exists, or a malformed one.
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

    def put(
        self, program: MigrationProgram, *, expected_version: int | None = None
    ) -> MigrationProgram:
        """Save ``program`` and return it as stored.

        The store owns ``version``: a new program is saved as 1 and every later save adds 1,
        whatever ``program.version`` says. With ``expected_version`` the save happens only
        if the stored version still equals it; otherwise ``PreconditionFailed`` is raised and
        nothing is written. The read of the stored version, the compare and the write run
        in one ``BEGIN IMMEDIATE`` transaction, so two writers that read the same version
        cannot both save, even through separate connections.
        """

        if not program.program_id:
            program = program.model_copy(update={"program_id": uuid4().hex[:12]})
        now = self._now()
        if not program.created_at:
            program = program.model_copy(update={"created_at": now})
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT payload FROM programs WHERE program_id = ? AND tenant_id = ?",
                    (program.program_id, program.tenant_id),
                ).fetchone()
                stored = None if row is None else _stored_version(row["payload"])
                if expected_version is not None and stored != expected_version:
                    raise PreconditionFailed(
                        f"program version {expected_version} is not the stored version",
                        details={"stored_version": stored},
                    )
                version = 1 if stored is None else stored + 1
                program = program.model_copy(update={"updated_at": now, "version": version})
                payload = json.dumps(program.model_dump(mode="json"))
                if row is None:
                    self._conn.execute(
                        """
                        INSERT INTO programs(program_id, tenant_id, status, payload, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            program.program_id,
                            program.tenant_id,
                            program.status,
                            payload,
                            program.updated_at,
                        ),
                    )
                else:
                    self._conn.execute(
                        """
                        UPDATE programs SET status = ?, payload = ?, updated_at = ?
                        WHERE program_id = ? AND tenant_id = ?
                        """,
                        (
                            program.status,
                            payload,
                            program.updated_at,
                            program.program_id,
                            program.tenant_id,
                        ),
                    )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        return program

    def get(self, program_id: str, *, tenant_id: str) -> MigrationProgram | None:
        with self._lock:
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
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [MigrationProgram.model_validate(json.loads(r["payload"])) for r in rows]


def _stored_version(payload: str) -> int:
    """Version of a stored payload. Rows saved before programs had a version read as 1."""

    return int(json.loads(payload).get("version", 1))
