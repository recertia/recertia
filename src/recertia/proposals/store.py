"""JSONL/SQLite-backed durable proposal queue (console C1)."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from recertia.persistence.db import open_db

ProposalStatus = Literal[
    "pending", "approved", "rejected", "request_changes", "superseded"
]


@dataclass
class ProposalRecord:
    proposal_id: str
    kind: str
    skill_id: str
    version: int
    rationale: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: ProposalStatus = "pending"
    created_at: str = ""
    created_by_job: str | None = None
    created_by_run: str | None = None
    tenant_id: str = "default"
    git_pr_url: str | None = None
    decision: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProposalStore:
    """Proposals in one SQLite file, on one connection that every thread shares.

    ``get``, ``list``, ``add`` and ``decide``'s write each hold ``_lock``, reads included: the
    connection's statement cache hands the same prepared statement to every ``execute`` of the
    same SQL text, so two threads stepping it at once reset each other's statement (REC-114).
    ``decide`` writes only while the row is still ``pending``, so of two decisions at once,
    through one store or two, exactly one wins.
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
                CREATE TABLE IF NOT EXISTS proposals (
                    proposal_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    skill_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def close(self) -> None:
        self._conn.close()

    def add(self, record: ProposalRecord) -> ProposalRecord:
        if not record.proposal_id:
            record.proposal_id = uuid4().hex[:12]
        if not record.created_at:
            record.created_at = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO proposals(
                    proposal_id, tenant_id, kind, skill_id, version, status, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.proposal_id,
                    record.tenant_id,
                    record.kind,
                    record.skill_id,
                    record.version,
                    record.status,
                    json.dumps(record.to_dict()),
                    record.created_at,
                ),
            )
        return record

    def get(self, proposal_id: str, *, tenant_id: str) -> ProposalRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM proposals WHERE proposal_id = ? AND tenant_id = ?",
                (proposal_id, tenant_id),
            ).fetchone()
        if row is None:
            return None
        return ProposalRecord(**json.loads(row["payload"]))

    def list(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[ProposalRecord]:
        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if status:
            clauses.append("status = ?")
            params.append(status)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        params.append(max(1, min(limit, 100)))
        sql = (
            "SELECT payload FROM proposals WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [ProposalRecord(**json.loads(r["payload"])) for r in rows]

    def decide(
        self,
        proposal_id: str,
        *,
        tenant_id: str,
        decision: str,
        actor: str,
        note: str = "",
    ) -> ProposalRecord:
        rec = self.get(proposal_id, tenant_id=tenant_id)
        if rec is None:
            raise KeyError(proposal_id)
        if rec.status != "pending":
            raise ValueError(f"proposal {proposal_id} is not pending ({rec.status})")
        if decision not in {"approve", "reject", "request_changes"}:
            raise ValueError(f"unknown decision {decision!r}")
        status: ProposalStatus = (
            "approved"
            if decision == "approve"
            else "rejected"
            if decision == "reject"
            else "request_changes"
        )
        rec.status = status
        rec.decision = {
            "decision": decision,
            "actor": actor,
            "note": note,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock, self._conn:
            changed = self._conn.execute(
                "UPDATE proposals SET status = ?, payload = ?"
                " WHERE proposal_id = ? AND tenant_id = ? AND status = 'pending'",
                (rec.status, json.dumps(rec.to_dict()), proposal_id, tenant_id),
            ).rowcount
        if changed != 1:
            # Another decision landed between the read above and this write, or the row is gone.
            now = self.get(proposal_id, tenant_id=tenant_id)
            if now is None:
                raise KeyError(proposal_id)
            raise ValueError(f"proposal {proposal_id} is not pending ({now.status})")
        return rec
