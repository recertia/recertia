"""Draft a candidate skill from a shipped TrajectoryImport. Never writes approved (ADR-0019).

Binds to ``contracts.trajectory_import.TrajectoryImport`` (ProvenanceBundle.source,
snake ComputerUseTaskClass on the CLI, kebab SkillVersion.task_class). There is no
``Policy.external_trajectory_import`` flag.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from contracts.criteria import SkillCertificationCriterion, mint_rejecting_proof
from contracts.policy import COMPUTER_USE_TASK_CLASSES
from contracts.skill import Hygiene, Provenance, SkillVersion, Step
from contracts.trajectory_import import TrajectoryImport, import_may_promote
from recertia.memory.procedural.hygiene import require_clean
from recertia.memory.procedural.store import SkillStore

_NOW = datetime(2026, 8, 24, tzinfo=timezone.utc)
_SKILL_ID_SAFE = re.compile(r"[^a-z0-9-]+")
_NOOP_ACTIONS = frozenset({"true", "open", "click", "type", "scroll", "wait", "navigate"})


class DistillRejected(ValueError):
    """Import cannot become a candidate (not reexecutable, or no replayable steps)."""


def skill_id_for_import(import_id: str) -> str:
    kebab = _SKILL_ID_SAFE.sub("-", import_id.lower()).strip("-")
    return f"import-{kebab or 'unnamed'}"


def skill_task_class(task_class: str) -> str:
    """Map snake Goal/golden class onto kebab SkillVersion.task_class."""

    return task_class.replace("_", "-")


def distill_imported(
    imported: TrajectoryImport,
    store: SkillStore,
    *,
    actor: str = "import-distill",
    task_class: str,
) -> SkillVersion:
    """Author a *candidate* from an external trajectory. Promotion stays outside this path."""

    if task_class not in COMPUTER_USE_TASK_CLASSES:
        raise DistillRejected(
            f"task_class {task_class!r} is not a computer-use golden class "
            f"{COMPUTER_USE_TASK_CLASSES}"
        )
    may, reason = import_may_promote(imported)
    if not imported.reexecutable:
        raise DistillRejected(
            "reexecutable=false: episodic only; cannot distill until a Recertia re-validation path exists"
        )
    if not imported.require_auditor_reverify:
        raise DistillRejected("require_auditor_reverify is false; imported claims cannot be auditor truth")
    secrets = _scan_payload(imported)
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
        cmd = (step.input or step.action or "").strip()
        if not cmd or cmd == "true" or cmd.lower() in _NOOP_ACTIONS:
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
    if not may:
        # Still a candidate: import_may_promote is the promotion bar, not the draft bar.
        # Missing criteria already refused above; remaining reasons stay informational.
        _ = reason
    version = SkillVersion(
        skill_id=skill_id_for_import(imported.import_id),
        version=1,
        title=f"Imported {imported.source} {imported.import_id}"[:120],
        intent=(
            f"Replay imported trajectory {imported.import_id} when Recertia re-validates it "
            "under locked criteria; not a standing Bot."
        ),
        task_class=skill_task_class(task_class),
        tags=["imported", imported.source.replace("_", "-")],
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
    task_class: str,
) -> SkillVersion:
    imported = TrajectoryImport.model_validate_json(path.read_text(encoding="utf-8"))
    return distill_imported(imported, store, actor=actor, task_class=task_class)


def _scan_payload(imported: TrajectoryImport) -> list[str]:
    from recertia.memory.procedural.hygiene import scan_findings

    return scan_findings(imported.model_dump_json())
