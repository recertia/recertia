"""Durable ``run_id`` registry in ``runtime.db`` (REL-3 F9)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recertia.persistence.db import open_db_with_init
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

# Keyset order for the run list (REL-6): newest first, ties broken by run id.
_LIST_INDEX = """
CREATE INDEX IF NOT EXISTS runs_list ON runs (tenant_id, created_at DESC, run_id DESC)
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
        self._conn = open_db_with_init(self.path, self._set_up)

    def _set_up(self, conn: sqlite3.Connection) -> None:
        """Post-connect steps; ``open_db_with_init`` closes ``conn`` if one raises."""

        conn.row_factory = sqlite3.Row
        with conn:
            conn.execute(_SCHEMA)
            conn.execute(_LIST_INDEX)

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
        tenant_id: str,
        *,
        limit: int,
        created_at_before: str | None = None,
        run_id_before: str | None = None,
        created_at_after: str | None = None,
        run_id_after: str | None = None,
    ) -> list[dict[str, object]]:
        """Up to ``limit`` rows of one tenant, ``created_at DESC, run_id DESC``.

        With both ``created_at_before`` and ``run_id_before`` set, only rows strictly after
        that position in this order are returned. With both ``created_at_after`` and
        ``run_id_after`` set, only rows strictly before that position are returned (a
        floor: ``created_at_after`` may be ``""``, which is below every dated row). Both
        positions must be written the way :meth:`reserve` writes ``created_at`` (UTC ISO
        text), so that text order is time order. The keyset bounds are row-value
        comparisons, which the ``runs_list`` index serves with a seek (SQLite 3.15 or newer).
        """

        sql = "SELECT run_id, status, summary_json, created_at, updated_at FROM runs WHERE tenant_id = ?"
        params: list[object] = [tenant_id]
        if created_at_before is not None and run_id_before is not None:
            sql += " AND (created_at, run_id) < (?, ?)"
            params += [created_at_before, run_id_before]
        if created_at_after is not None and run_id_after is not None:
            sql += " AND (created_at, run_id) > (?, ?)"
            params += [created_at_after, run_id_after]
        sql += " ORDER BY created_at DESC, run_id DESC LIMIT ?"
        params.append(limit)
        return [_listing_row(row) for row in self._conn.execute(sql, params).fetchall()]

    def get_many(self, tenant_id: str, run_ids: Sequence[str]) -> dict[str, dict[str, object]]:
        """The listing rows of ``run_ids`` that exist for the tenant, keyed by run id."""

        found: dict[str, dict[str, object]] = {}
        for start in range(0, len(run_ids), _IN_CHUNK):
            chunk = list(run_ids[start : start + _IN_CHUNK])
            marks = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                "SELECT run_id, status, summary_json, created_at, updated_at "
                f"FROM runs WHERE tenant_id = ? AND run_id IN ({marks})",
                [tenant_id, *chunk],
            ).fetchall()
            for row in rows:
                found[str(row["run_id"])] = _listing_row(row)
        return found


_IN_CHUNK = 500


def _listing_row(row: sqlite3.Row) -> dict[str, object]:
    """A run row as the list returns it. A summary that is not a JSON object counts as empty."""

    try:
        summary = json.loads(row["summary_json"] or "{}")
    except json.JSONDecodeError:
        summary = {}
    if not isinstance(summary, dict):
        summary = {}
    return {
        **summary,
        "run_id": row["run_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
