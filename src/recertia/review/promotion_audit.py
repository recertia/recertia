"""Operator-only promotion audit store (REC-009, Gap A).

A golden-gate refusal records its full detail here: every failing fixture id, golden
path, result row and run detail. Proposers never see any of it. They get a
decision-only :class:`~recertia.review.promotion.PromotionError` and an opaque
``audit_ref``, never a path.

The store is a SQLite file under an operator root set by ``RECERTIA_OPERATOR_AUDIT_ROOT``.
It fails closed:

* an unset root raises :class:`AuditSinkUnavailable`; there is no default location
  and the detail never falls back to a proposer-readable place;
* a root inside a git checkout, or inside any caller-supplied forbidden root (the
  API root, which holds every tenant ``runs_root``, or the promote ``runs_root``),
  is refused.

Reading is an operator action (:meth:`OperatorAuditStore.read`). No proposer plane
(``recertia.nodes``, ``recertia.distill``, ``recertia.solver``, the
``recertia.jobs.workers`` miner) may import this module; a boundary test enforces it.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recertia.persistence.db import open_db

__all__ = [
    "AuditSinkUnavailable",
    "OperatorAuditStore",
    "open_operator_audit",
]

AUDIT_FILENAME = "promotion_audit.sqlite"


class AuditSinkUnavailable(Exception):
    """The operator audit root is unset or sits somewhere proposers can read."""


_ENTRY_KEYS = ("skill_id", "version", "refusal_kind", "failing_fixtures", "results", "detail")


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _git_checkout_above(path: Path) -> bool:
    return any((candidate / ".git").exists() for candidate in (path, *path.parents))


class OperatorAuditStore:
    """SQLite audit sink under an operator root. Connections are per call.

    Satisfies :class:`recertia.review.promotion.PromotionAuditSink`. The promotion
    module depends only on that protocol, so importing promotion never pulls this
    module into a proposer plane.
    """

    def __init__(
        self,
        root: Path | str | None,
        *,
        forbidden_roots: Iterable[Path | str] = (),
    ) -> None:
        if root is None or not str(root).strip():
            raise AuditSinkUnavailable("operator audit root is unset")
        real = Path(os.path.realpath(Path(root).expanduser()))
        if _git_checkout_above(real):
            raise AuditSinkUnavailable("operator audit root must be outside any git checkout")
        for forbidden in forbidden_roots:
            if _inside(real, Path(os.path.realpath(forbidden))):
                raise AuditSinkUnavailable(
                    "operator audit root must be outside the API root and tenant runs roots"
                )
        real.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = real
        self.path = real / AUDIT_FILENAME
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS promotion_audit (
                        audit_ref TEXT PRIMARY KEY,
                        recorded_at TEXT NOT NULL,
                        skill_id TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        refusal_kind TEXT NOT NULL,
                        payload TEXT NOT NULL
                    )
                    """
                )
        finally:
            conn.close()
        os.chmod(self.path, 0o600)

    def _connect(self) -> Any:
        return open_db(self.path)

    def record(self, entry: Mapping[str, Any]) -> str:
        """Store one refusal; return an opaque ``audit_ref`` (random, not content-derived)."""

        unknown = set(entry) - set(_ENTRY_KEYS)
        if unknown:
            raise ValueError(f"unknown audit entry keys: {sorted(unknown)}")
        audit_ref = f"pa-{uuid.uuid4().hex}"
        payload = {
            "skill_id": str(entry.get("skill_id", "")),
            "version": int(entry.get("version", 0)),
            "refusal_kind": str(entry.get("refusal_kind", "")),
            "failing_fixtures": [str(x) for x in entry.get("failing_fixtures", [])],
            "results": list(entry.get("results", [])),
            "detail": str(entry.get("detail", "")),
        }
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO promotion_audit(audit_ref, recorded_at, skill_id, version, "
                    "refusal_kind, payload) VALUES (?,?,?,?,?,?)",
                    (
                        audit_ref,
                        datetime.now(timezone.utc).isoformat(),
                        payload["skill_id"],
                        payload["version"],
                        payload["refusal_kind"],
                        json.dumps(payload, default=str),
                    ),
                )
        finally:
            conn.close()
        return audit_ref

    def read(self, audit_ref: str) -> dict[str, Any] | None:
        """Operator read. Returns the full record, or ``None`` for an unknown ref."""

        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT recorded_at, payload FROM promotion_audit WHERE audit_ref = ?",
                (audit_ref,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        record = json.loads(row[1])
        record["audit_ref"] = audit_ref
        record["recorded_at"] = row[0]
        return dict(record)


def open_operator_audit(
    root: Path | str | None,
    *,
    forbidden_roots: Iterable[Path | str] = (),
) -> OperatorAuditStore | None:
    """Open the sink, or return ``None`` when it is unset or refused.

    ``None`` means refusal detail is dropped, never written somewhere else.
    """

    try:
        return OperatorAuditStore(root, forbidden_roots=forbidden_roots)
    except AuditSinkUnavailable:
        return None
