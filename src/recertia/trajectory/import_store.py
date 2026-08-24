"""Append-only ingest of TrajectoryImport (2026-08-22 plan Phase 0).

Validated imports land under ``{runs_root}/runs/{tenant}/imports/{import_id}.json`` and a
matching episodic case. This path never writes approved skills. Imported
claims are never auditor truth; ``import_may_promote`` is informational.
Distill → review → control-arm is Phase 1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from contracts.trajectory_import import TrajectoryImport, import_may_promote
from recertia.memory.episodic import CaseRecord, EpisodicStore
from recertia.paths import PathEscapeError, contained_path

_IMPORT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ImportRejected(ValueError):
    """Provenance, identity, or append-only collision rejected the import."""


@dataclass(frozen=True)
class ImportResult:
    import_id: str
    case_id: str
    stored_path: str
    reexecutable: bool
    may_promote: bool
    promote_reason: str
    promoted: bool = False
    proposal_id: str | None = None

    def as_public_dict(self) -> dict[str, Any]:
        """CLI and HTTP ingest payload. ``promoted`` is always false on this path."""

        return {
            "import_id": self.import_id,
            "case_id": self.case_id,
            "stored_path": self.stored_path,
            "reexecutable": self.reexecutable,
            "may_promote": self.may_promote,
            "promote_reason": self.promote_reason,
            "proposal_id": self.proposal_id,
            "promoted": False,
        }


def _outcome(value: str) -> Literal["solved", "failed", "abandoned"]:
    if value == "solved":
        return "solved"
    if value == "failed":
        return "failed"
    return "abandoned"


def ingest_trajectory(
    payload: dict | TrajectoryImport,
    *,
    runs_root: Path | str,
    tenant_id: str = "default",
    actor: str = "import",
) -> ImportResult:
    """Validate, persist append-only, write episodic. Never promotes."""

    imported = (
        payload
        if isinstance(payload, TrajectoryImport)
        else TrajectoryImport.model_validate(payload)
    )
    if not _IMPORT_ID_RE.match(imported.import_id):
        raise ImportRejected(
            "import_id must be 1–64 chars of [A-Za-z0-9._-] starting alphanumeric"
        )

    root = Path(runs_root)
    tenant_root = root / "runs" / tenant_id
    imports_dir = tenant_root / "imports"
    imports_dir.mkdir(parents=True, exist_ok=True)
    try:
        dest = contained_path(imports_dir, f"{imported.import_id}.json")
    except PathEscapeError as exc:
        raise ImportRejected(f"import_id escapes imports dir: {imported.import_id!r}") from exc
    if dest.exists():
        raise ImportRejected(f"import {imported.import_id!r} already exists (append-only)")
    dest.write_text(imported.model_dump_json(indent=2) + "\n", encoding="utf-8")

    case_id = f"import-{imported.import_id}"
    case = CaseRecord(
        case_id=case_id,
        run_id=f"import:{imported.import_id}",
        attempt_no=0,
        request_excerpt=imported.source_ref[:240],
        outcome=_outcome(imported.outcome),
        transcript_ref=str(dest),
        artifacts=[a.ref for a in imported.artifacts],
        approach=f"external:{imported.source}",
        session_id=imported.import_id,
        recorded_at=datetime.now(timezone.utc),
    )
    EpisodicStore(tenant_root / "episodic").write(case)

    may_promote, promote_reason = import_may_promote(imported)
    proposal_id = None
    if may_promote:
        from recertia.proposals.store import ProposalRecord, ProposalStore

        store = ProposalStore(tenant_root / "proposals.sqlite")
        try:
            rec = store.add(
                ProposalRecord(
                    proposal_id=uuid4().hex[:12],
                    kind="external_trajectory",
                    skill_id=f"import-{imported.import_id}",
                    version=0,
                    rationale=(
                        "Imported trajectory queued for Recertia re-validation. "
                        "Not approved. Control-arm lift still required."
                    ),
                    payload={
                        "import_id": imported.import_id,
                        "source": imported.source,
                        "reexecutable": True,
                        "promoted": False,
                        "actor": actor,
                        "promote_reason": promote_reason,
                    },
                    tenant_id=tenant_id,
                    created_by_job="trajectory-import",
                    created_by_run=f"import:{imported.import_id}",
                )
            )
            proposal_id = rec.proposal_id
        finally:
            store.close()

    return ImportResult(
        import_id=imported.import_id,
        case_id=case_id,
        stored_path=str(dest),
        reexecutable=imported.reexecutable,
        may_promote=may_promote,
        promote_reason=promote_reason,
        promoted=False,
        proposal_id=proposal_id,
    )
