"""Append-only ingest of TrajectoryImport into episodic + pending proposals (ADR-0019)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from contracts.policy import Policy
from contracts.trajectory_import import TrajectoryImport
from recertia.memory.episodic import CaseRecord, EpisodicStore
from recertia.paths import contained_path
from recertia.policy_load import load_policy
from recertia.proposals.store import ProposalRecord, ProposalStore


class ImportRejected(ValueError):
    """Provenance, policy, or environment rejected the import."""


@dataclass(frozen=True)
class ImportResult:
    import_id: str
    case_id: str
    stored_path: str
    proposal_id: str | None
    reexecutable: bool
    promoted: bool = False


def _outcome(value: str) -> str:
    if value == "solved":
        return "solved"
    if value == "failed":
        return "failed"
    return "abandoned"


def _kebab(value: str | None) -> str | None:
    if value is None:
        return None
    return value.replace("_", "-")


def ingest_trajectory(
    payload: dict | TrajectoryImport,
    *,
    runs_root: Path | str,
    tenant_id: str = "default",
    policy: Policy | None = None,
    actor: str = "import",
) -> ImportResult:
    """Validate, persist, write episodic. Never writes approved state."""

    policy = policy or load_policy()
    if not policy.improvement.external_trajectory_import:
        raise ImportRejected("improvement.external_trajectory_import is false")
    imported = (
        payload
        if isinstance(payload, TrajectoryImport)
        else TrajectoryImport.model_validate(payload)
    )
    root = Path(runs_root)
    tenant_root = root / "runs" / tenant_id
    imports_dir = tenant_root / "imports"
    imports_dir.mkdir(parents=True, exist_ok=True)
    dest = contained_path(imports_dir, f"{imported.import_id}.json")
    if dest.exists():
        raise ImportRejected(f"import {imported.import_id!r} already exists (append-only)")
    dest.write_text(imported.model_dump_json(indent=2) + "\n", encoding="utf-8")

    case_id = f"import-{imported.import_id}"
    case = CaseRecord(
        case_id=case_id,
        run_id=f"import:{imported.import_id}",
        attempt_no=0,
        task_class=_kebab(imported.task_class),
        request_excerpt=imported.source_ref[:240],
        outcome=_outcome(imported.outcome),
        transcript_ref=str(dest),
        artifacts=[a.ref for a in imported.artifacts],
        approach=f"external:{imported.source}",
        session_id=imported.import_id,
        recorded_at=datetime.now(timezone.utc),
    )
    episodic = EpisodicStore(tenant_root / "episodic")
    episodic.write(case)

    proposal_id = None
    if imported.reexecutable:
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
        proposal_id=proposal_id,
        reexecutable=imported.reexecutable,
        promoted=False,
    )
