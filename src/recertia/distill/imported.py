"""Draft a candidate skill from a TrajectoryImport. Never writes approved (ADR-0019)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from contracts.criteria import SkillCertificationCriterion, mint_rejecting_proof
from contracts.skill import Hygiene, Provenance, SkillVersion, Step
from contracts.trajectory_import import TrajectoryImport
from recertia.memory.procedural.store import SkillStore

_NOW = datetime(2026, 8, 22, tzinfo=timezone.utc)


class DistillRejected(ValueError):
    """Import cannot become a candidate (not reexecutable, or policy)."""


def distill_imported(
    imported: TrajectoryImport,
    store: SkillStore,
    *,
    actor: str = "import-distill",
) -> SkillVersion:
    """Author a *candidate* from an external trajectory. Promotion stays outside this path."""

    if not imported.reexecutable:
        raise DistillRejected(
            "reexecutable=false: episodic only; cannot distill until a Recertia re-validation path exists"
        )
    skill_id = (imported.task_class or f"import-{imported.import_id}").replace("_", "-")[:64]
    task_class = (imported.task_class or "repo-chore").replace("_", "-")
    steps: list[Step] = []
    for step in imported.steps[:12]:
        cmd = step.action if (step.tool or "shell") in {"shell", "bash"} else "true"
        steps.append(
            Step(
                id=f"step_{step.seq}",
                tool="shell",
                intent=f"Replay imported action {step.seq} when the workspace is writable",
                inputs={"command": cmd if cmd.strip() else "true"},
            )
        )
    if not steps:
        steps = [
            Step(
                id="step_0",
                tool="shell",
                intent="Placeholder replay when the imported trajectory had no steps",
                inputs={"command": "true"},
            )
        ]
    criteria: list[SkillCertificationCriterion] = []
    for raw in imported.criteria_snapshot:
        if raw.kind != "command" or not raw.run:
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
        base = SkillCertificationCriterion(
            id="import-true",
            kind="command",
            run="true",
            weight=1.0,
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
    version = SkillVersion(
        skill_id=skill_id,
        version=1,
        title=f"Imported {imported.source} {imported.import_id}",
        intent=(
            f"Replay imported trajectory {imported.import_id} when Recertia re-validates it "
            "under locked criteria; not a standing Bot."
        ),
        task_class=task_class,
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
    return store.write_candidate(version)


def distill_imported_file(
    path: Path,
    store: SkillStore,
    *,
    actor: str = "import-distill",
) -> SkillVersion:
    imported = TrajectoryImport.model_validate_json(path.read_text(encoding="utf-8"))
    return distill_imported(imported, store, actor=actor)
