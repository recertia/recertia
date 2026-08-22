"""Draft a candidate skill from a TrajectoryImport. Never writes approved (ADR-0019)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from contracts.criteria import SkillCertificationCriterion, mint_rejecting_proof
from contracts.policy import Policy
from contracts.skill import Hygiene, Provenance, SkillVersion, Step
from contracts.trajectory_import import TrajectoryImport
from recertia.memory.procedural.hygiene import require_clean, scan_findings
from recertia.memory.procedural.store import SkillStore
from recertia.policy_load import load_policy

_NOW = datetime(2026, 8, 22, tzinfo=timezone.utc)


class DistillRejected(ValueError):
    """Import cannot become a candidate (not reexecutable, or policy)."""


def distill_imported(
    imported: TrajectoryImport,
    store: SkillStore,
    *,
    actor: str = "import-distill",
    policy: Policy | None = None,
) -> SkillVersion:
    """Author a *candidate* from an external trajectory. Promotion stays outside this path."""

    policy = policy or load_policy()
    if not policy.improvement.external_trajectory_import:
        raise DistillRejected("improvement.external_trajectory_import is false")
    if not imported.reexecutable:
        raise DistillRejected(
            "reexecutable=false: episodic only; cannot distill until a Recertia re-validation path exists"
        )
    secrets = scan_findings(imported.model_dump_json())
    if secrets:
        raise DistillRejected(f"hygiene scan failed ({', '.join(secrets)})")
    criteria: list[SkillCertificationCriterion] = []
    for raw in imported.criteria_snapshot:
        run = (raw.run or "").strip()
        if raw.kind != "command" or not run or run == "true":
            continue
        base = SkillCertificationCriterion(
            id=raw.id,
            kind="command",
            run=raw.run,
            expect_exit=raw.expect_exit,
            weight=raw.weight or 1.0,
            preregistered=True,
            authored_by="distiller",
        )
        criteria.append(
            base.model_copy(
                update={
                    "sensitivity_proof": mint_rejecting_proof(
                        base, negative_fixture="import-empty", fingerprint="import-neg"
                    )
                }
            )
        )
    if not criteria:
        raise DistillRejected(
            "no command criterion on the import; refuse to author a true-noop skill"
        )
    steps: list[Step] = []
    for step in imported.steps[:12]:
        if (step.tool or "shell") not in {"shell", "bash"}:
            continue
        cmd = (step.action or "").strip()
        if not cmd or cmd == "true":
            continue
        steps.append(
            Step(
                id=f"step_{step.seq}",
                tool="shell",
                intent=f"Replay imported action {step.seq} when the workspace is writable",
                inputs={"command": cmd},
            )
        )
    if not steps:
        raise DistillRejected("no replayable shell steps; refuse to author a true-noop skill")
    version = SkillVersion(
        skill_id=imported.imported_skill_id,
        version=1,
        title=f"Imported {imported.source} {imported.import_id}",
        intent=(
            f"Replay imported trajectory {imported.import_id} when Recertia re-validates it "
            "under locked criteria; not a standing Bot."
        ),
        task_class=imported.task_class_kebab or "repo-chore",
        tags=["imported", imported.source],
        steps=steps,
        certification_criteria=criteria,
        provenance=Provenance(
            distilled_from_run=f"import:{imported.import_id}",
            distilled_at=_NOW,
            authored_by=actor,
            curation="mined_from_human_artifact",
            derivation="mined_artifact",
            source_run_ids=[f"import:{imported.import_id}"],
        ),
        hygiene=Hygiene(secret_scan="passed", scanned_at=_NOW),
    )
    return store.write_candidate(require_clean(version))


def distill_imported_file(
    path: Path,
    store: SkillStore,
    *,
    actor: str = "import-distill",
    policy: Policy | None = None,
) -> SkillVersion:
    imported = TrajectoryImport.model_validate_json(path.read_text(encoding="utf-8"))
    return distill_imported(imported, store, actor=actor, policy=policy)
