"""SQLite ``SessionRepo`` and ``TenantRepo`` on ``control.db``."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from recertia.persistence.db import open_db
from recertia.ports.identity import TenantId


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteUnitOfWork:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()


class SqliteSessionRepo:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn = open_db(path)
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oidc_pending (
                state TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def put(
        self,
        session_id: str,
        payload: Mapping[str, object],
        *,
        tenant: TenantId,
        expires_at: datetime,
    ) -> None:
        self._conn.execute(
            """INSERT INTO sessions(session_id, tenant_id, payload, expires_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 tenant_id=excluded.tenant_id,
                 payload=excluded.payload,
                 expires_at=excluded.expires_at""",
            (session_id, str(tenant), json.dumps(dict(payload)), expires_at.isoformat()),
        )
        self._conn.commit()

    def get(self, session_id: str) -> Mapping[str, object] | None:
        row = self._conn.execute(
            "SELECT payload, expires_at FROM sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        if row[1] < _now():
            self.delete(session_id)
            return None
        parsed = json.loads(row[0])
        return parsed if isinstance(parsed, dict) else None

    def delete(self, session_id: str) -> None:
        self._conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
        self._conn.commit()

    def put_oidc_pending(
        self, state: str, payload: Mapping[str, object], *, expires_at: datetime
    ) -> None:
        self._conn.execute(
            """INSERT INTO oidc_pending(state, payload, expires_at) VALUES (?, ?, ?)
               ON CONFLICT(state) DO UPDATE SET payload=excluded.payload, expires_at=excluded.expires_at""",
            (state, json.dumps(dict(payload)), expires_at.isoformat()),
        )
        self._conn.commit()

    def pop_oidc_pending(self, state: str) -> Mapping[str, object] | None:
        row = self._conn.execute(
            "SELECT payload FROM oidc_pending WHERE state=?",
            (state,),
        ).fetchone()
        self._conn.execute("DELETE FROM oidc_pending WHERE state=?", (state,))
        self._conn.commit()
        if row is None:
            return None
        parsed = json.loads(row[0])
        return parsed if isinstance(parsed, dict) else None


class SqliteTenantRepo:
    def __init__(self, path: Path) -> None:
        self._conn = open_db(path)
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tenants (
                tenant_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memberships (
                tenant_id TEXT NOT NULL,
                principal_id TEXT NOT NULL,
                role TEXT NOT NULL,
                PRIMARY KEY (tenant_id, principal_id)
            );
            """
        )
        self._conn.commit()

    def upsert(self, tenant: TenantId, *, display_name: str, status: str) -> None:
        self._conn.execute(
            """INSERT INTO tenants(tenant_id, display_name, status) VALUES (?, ?, ?)
               ON CONFLICT(tenant_id) DO UPDATE SET
                 display_name=excluded.display_name,
                 status=excluded.status""",
            (str(tenant), display_name, status),
        )
        self._conn.commit()

    def get(self, tenant: TenantId) -> Mapping[str, object] | None:
        row = self._conn.execute(
            "SELECT tenant_id, display_name, status FROM tenants WHERE tenant_id=?",
            (str(tenant),),
        ).fetchone()
        if row is None:
            return None
        return {"tenant_id": row[0], "display_name": row[1], "status": row[2]}

    def grant(self, tenant: TenantId, principal_id: str, role: str) -> None:
        if role not in {"tenant_admin", "operator"}:
            raise ValueError("membership role must be tenant_admin or operator")
        self._conn.execute(
            """INSERT INTO memberships(tenant_id, principal_id, role) VALUES (?, ?, ?)
               ON CONFLICT(tenant_id, principal_id) DO UPDATE SET role=excluded.role""",
            (str(tenant), principal_id, role),
        )
        self._conn.commit()

    def role_for(self, tenant: TenantId, principal_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT role FROM memberships WHERE tenant_id=? AND principal_id=?",
            (str(tenant), principal_id),
        ).fetchone()
        return None if row is None else str(row[0])

    def list_ids(self) -> list[TenantId]:
        rows = self._conn.execute("SELECT tenant_id FROM tenants ORDER BY tenant_id").fetchall()
        return [TenantId(row[0]) for row in rows]
