"""SQLite-backed registered workspace store."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from contracts.workspace import RegisteredWorkspace
from recertia.paths import (
    HostRootError,
    assert_host_root_allowed,
    normalize_host_root,
    validate_workspace_id,
)
from recertia.persistence.db import open_db_with_init


class WorkspaceRegistry:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = open_db_with_init(self.path, self._set_up)

    def _set_up(self, conn: sqlite3.Connection) -> None:
        """Post-connect steps; ``open_db_with_init`` closes ``conn`` if one raises."""

        conn.row_factory = sqlite3.Row
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workspaces (
                    tenant_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    host_root TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    notes TEXT,
                    PRIMARY KEY (tenant_id, workspace_id)
                )
                """
            )

    def close(self) -> None:
        self._conn.close()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _row_to_model(self, row: sqlite3.Row) -> RegisteredWorkspace:
        return RegisteredWorkspace(
            workspace_id=row["workspace_id"],
            tenant_id=row["tenant_id"],
            display_name=row["display_name"],
            host_root=row["host_root"],
            enabled=bool(row["enabled"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            created_by=row["created_by"],
            notes=row["notes"],
        )

    def register(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        display_name: str,
        host_root: str,
        created_by: str,
        notes: str | None = None,
    ) -> RegisteredWorkspace:
        workspace_id = validate_workspace_id(workspace_id)
        try:
            normalized = assert_host_root_allowed(
                normalize_host_root(host_root, must_exist=True)
            )
        except HostRootError:
            raise
        now = self._now()
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT workspace_id FROM workspaces WHERE tenant_id = ? AND workspace_id = ?",
                (tenant_id, workspace_id),
            ).fetchone()
            if existing is not None:
                raise LookupError(f"workspace_id exists: {workspace_id}")
            self._conn.execute(
                """
                INSERT INTO workspaces(
                    tenant_id, workspace_id, display_name, host_root,
                    enabled, created_at, created_by, notes
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    tenant_id,
                    workspace_id,
                    display_name.strip(),
                    normalized,
                    now,
                    created_by,
                    notes,
                ),
            )
        return RegisteredWorkspace(
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            display_name=display_name.strip(),
            host_root=normalized,
            enabled=True,
            created_at=datetime.fromisoformat(now),
            created_by=created_by,
            notes=notes,
        )

    def get(
        self, workspace_id: str, *, tenant_id: str, enabled_only: bool = False
    ) -> RegisteredWorkspace | None:
        row = self._conn.execute(
            """
            SELECT * FROM workspaces
            WHERE tenant_id = ? AND workspace_id = ?
            """,
            (tenant_id, workspace_id),
        ).fetchone()
        if row is None:
            return None
        ws = self._row_to_model(row)
        if enabled_only and not ws.enabled:
            return None
        return ws

    def list(self, *, tenant_id: str) -> list[RegisteredWorkspace]:
        rows = self._conn.execute(
            """
            SELECT * FROM workspaces WHERE tenant_id = ?
            ORDER BY workspace_id ASC
            """,
            (tenant_id,),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def set_enabled(
        self, workspace_id: str, *, tenant_id: str, enabled: bool
    ) -> RegisteredWorkspace | None:
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                UPDATE workspaces SET enabled = ?
                WHERE tenant_id = ? AND workspace_id = ?
                """,
                (1 if enabled else 0, tenant_id, workspace_id),
            )
            if cur.rowcount == 0:
                return None
        return self.get(workspace_id, tenant_id=tenant_id)

    def patch(
        self,
        workspace_id: str,
        *,
        tenant_id: str,
        display_name: str | None = None,
        notes: str | None = None,
        enabled: bool | None = None,
        clear_notes: bool = False,
    ) -> RegisteredWorkspace | None:
        ws = self.get(workspace_id, tenant_id=tenant_id)
        if ws is None:
            return None
        new_name = display_name.strip() if display_name is not None else ws.display_name
        if not new_name:
            raise ValueError("display_name must be non-empty")
        new_notes = ws.notes
        if clear_notes:
            new_notes = None
        elif notes is not None:
            new_notes = notes
        new_enabled = ws.enabled if enabled is None else enabled
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE workspaces
                SET display_name = ?, notes = ?, enabled = ?
                WHERE tenant_id = ? AND workspace_id = ?
                """,
                (
                    new_name,
                    new_notes,
                    1 if new_enabled else 0,
                    tenant_id,
                    workspace_id,
                ),
            )
        return self.get(workspace_id, tenant_id=tenant_id)
