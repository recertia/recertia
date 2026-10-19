"""SQLite-backed migration program store (Goal packs)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from contracts.program import MigrationProgram
from recertia.persistence.db import open_db
from recertia.runtime.errors import ProgramConflict


def _payload_hash(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


class ProgramStore:
    def __init__(self, path: Path | str, *, idempotency: Any | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._idempotency = idempotency
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
                    version INTEGER NOT NULL DEFAULT 1,
                    payload_hash TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (tenant_id, program_id)
                )
                """
            )
            _ensure_column(self._conn, "programs", "version", "INTEGER NOT NULL DEFAULT 1")
            _ensure_column(self._conn, "programs", "payload_hash", "TEXT NOT NULL DEFAULT ''")

    def close(self) -> None:
        self._conn.close()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def put(
        self,
        program: MigrationProgram,
        *,
        if_match: int | None = None,
        idempotency_key: str | None = None,
        idempotency_hash: str | None = None,
    ) -> MigrationProgram:
        if not program.program_id:
            program = program.model_copy(update={"program_id": uuid4().hex[:12]})
        now = self._now()
        if not program.created_at:
            program = program.model_copy(update={"created_at": now})
        digest = idempotency_hash
        if idempotency_key is not None and self._idempotency is not None:
            if digest is None:
                digest = _payload_hash(program.model_dump(mode="json"))
            replay = self._idempotency.check(
                program.tenant_id, "program.put", idempotency_key, digest
            )
            if replay is not None:
                return MigrationProgram.model_validate(replay["program"])

        with self._lock, self._conn:
            row = self._conn.execute(
                """
                SELECT version FROM programs
                WHERE tenant_id = ? AND program_id = ?
                """,
                (program.tenant_id, program.program_id),
            ).fetchone()
            if row is None:
                if if_match is not None:
                    raise ProgramConflict("program version does not match If-Match")
                version = 1
                program = program.model_copy(update={"updated_at": now, "version": version})
                payload = program.model_dump(mode="json")
                self._conn.execute(
                    """
                    INSERT INTO programs(
                        program_id, tenant_id, status, payload, updated_at, version, payload_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        program.program_id,
                        program.tenant_id,
                        program.status,
                        json.dumps(payload),
                        program.updated_at,
                        version,
                        _payload_hash(payload),
                    ),
                )
            else:
                current = int(row["version"])
                if if_match is not None and current != if_match:
                    raise ProgramConflict("program version does not match If-Match")
                version = current + 1
                program = program.model_copy(update={"updated_at": now, "version": version})
                payload = program.model_dump(mode="json")
                if if_match is None:
                    cur = self._conn.execute(
                        """
                        UPDATE programs SET
                            status=?, payload=?, updated_at=?, version=version+1, payload_hash=?
                        WHERE tenant_id=? AND program_id=?
                        """,
                        (
                            program.status,
                            json.dumps(payload),
                            program.updated_at,
                            _payload_hash(payload),
                            program.tenant_id,
                            program.program_id,
                        ),
                    )
                else:
                    cur = self._conn.execute(
                        """
                        UPDATE programs SET
                            status=?, payload=?, updated_at=?, version=?, payload_hash=?
                        WHERE tenant_id=? AND program_id=? AND version=?
                        """,
                        (
                            program.status,
                            json.dumps(payload),
                            program.updated_at,
                            version,
                            _payload_hash(payload),
                            program.tenant_id,
                            program.program_id,
                            if_match,
                        ),
                    )
                if cur.rowcount == 0:
                    raise ProgramConflict("program version does not match If-Match")

        if idempotency_key is not None and self._idempotency is not None and digest is not None:
            self._idempotency.put(
                program.tenant_id,
                "program.put",
                idempotency_key,
                digest,
                {"program": program.model_dump(mode="json")},
            )
        return program

    def get(self, program_id: str, *, tenant_id: str) -> MigrationProgram | None:
        row = self._conn.execute(
            """
            SELECT payload, version FROM programs
            WHERE program_id = ? AND tenant_id = ?
            """,
            (program_id, tenant_id),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        payload["version"] = int(row["version"])
        return MigrationProgram.model_validate(payload)

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
            "SELECT payload, version FROM programs WHERE "
            + " AND ".join(clauses)
            + " ORDER BY updated_at DESC LIMIT ?"
        )
        rows = self._conn.execute(sql, params).fetchall()
        items: list[MigrationProgram] = []
        for row in rows:
            payload = json.loads(row["payload"])
            payload["version"] = int(row["version"])
            items.append(MigrationProgram.model_validate(payload))
        return items
