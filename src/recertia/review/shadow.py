"""Shadow execution: offline comparison that never reaches the caller (M5)."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from contracts.criteria import CriterionResult, TaskCriterion
from contracts.run import RunManifest, RunState, SkillCandidateRef, Task
from recertia.evals.store import EvalStore
from recertia.memory.procedural.active_set import ShadowSlot, select_shadow_slots
from recertia.memory.procedural.store import SkillStore
from recertia.review.autonomy_config import DEFAULT_AUTONOMY, AutonomyConfig


@dataclass
class ShadowResult:
    skill_id: str
    version: int
    success: bool
    visible_to_caller: bool = False
    run_id: str | None = None


def record_shadow_outcome(
    store: SkillStore,
    skill_id: str,
    version: int,
    *,
    success: bool,
    run_id: str | None = None,
) -> ShadowResult:
    """Record an offline shadow outcome without bumping predictive trust.

    Shadow evidence lives in eval-store observations / contribution samples only;
    ``predictive_trust.applications`` / ``successes`` track caller-visible applies.
    """

    status = store.get_status(skill_id, version)
    if status.lifecycle not in ("shadow", "approved", "benched"):
        raise ValueError(f"shadow outcomes only for shadow/approved/benched; got {status.lifecycle}")
    # Do not write SkillStats here — scheduling persists arm=shadow observations via
    # ``persist_shadow_observation``; contribution is recomputed from those samples.
    return ShadowResult(
        skill_id=skill_id,
        version=version,
        success=success,
        visible_to_caller=False,
        run_id=run_id,
    )


def enter_shadow(store: SkillStore, skill_id: str, version: int) -> None:
    status = store.get_status(skill_id, version)
    if status.lifecycle not in ("candidate", "approved", "benched", "shadow", "draft"):
        raise ValueError(
            f"enter_shadow refused for lifecycle={status.lifecycle!r}; "
            "quarantined/retired versions cannot be shadowed"
        )
    store.write_status(
        status.model_copy(update={"lifecycle": "shadow", "active": False}),
        expected_lifecycle=status.lifecycle,
    )


def evaluate_shadow_offline(store: SkillStore, slot: ShadowSlot, *, workdir: Path) -> bool:
    """Score required non-judge command criteria in an isolated workdir (never caller-visible)."""

    from recertia.nodes.validate import criterion_runner

    version = store.get_version(slot.skill_id, slot.version)
    workdir.mkdir(parents=True, exist_ok=True)
    runner = criterion_runner()
    for criterion in version.certification_criteria:
        if criterion.kind != "command" or not criterion.is_required:
            continue
        if not runner.score(criterion, workdir=workdir).passed:
            return False
    return True


def persist_shadow_observation(
    eval_store: EvalStore,
    *,
    slot: ShadowSlot,
    success: bool,
    snapshot_id: str,
    run_id: str | None = None,
) -> None:
    """Append an arm=shadow observation derived from a locked offline RunState."""

    rid = run_id or f"shadow-{slot.skill_id}-v{slot.version}-{uuid4().hex[:12]}"
    criterion = TaskCriterion(
        id="shadow-offline",
        kind="command",
        run="true",
        source="caller",
        preregistered=True,
    )
    now = datetime.now(timezone.utc)
    state = RunState(
        run_id=rid,
        task=Task(
            task_id=rid,
            request=f"offline shadow slot for {slot.skill_id}@v{slot.version}",
            task_class=slot.task_class,
            submitted_at=now,
            is_eval_fixture=False,
        ),
        manifest=RunManifest(index_snapshot_id=snapshot_id, criteria_hash="shadow-offline"),
        arm="shadow",
        criteria=[criterion],
        criteria_locked_at=now,
        chosen=SkillCandidateRef(
            skill_id=slot.skill_id, version=slot.version, score=1.0, shadow=True
        ),
        attempt_no=1,
        results=[
            CriterionResult(
                criterion_id=criterion.id, kind="command", passed=success, weight=1.0
            )
        ],
        terminal="solved" if success else "unsolved",
    )
    eval_store.append_run(state)


def schedule_shadow_slots(
    store: SkillStore,
    *,
    eval_store: EvalStore | None = None,
    config: AutonomyConfig = DEFAULT_AUTONOMY,
    snapshot_id: str = "shadow-schedule",
    evaluate: Callable[[ShadowSlot, Path], bool] | None = None,
) -> list[ShadowResult]:
    """Select bounded offline slots, evaluate without caller-visible effects, persist outcomes."""

    slots = select_shadow_slots(store, config=config)
    evaluate = evaluate or (lambda slot, workdir: evaluate_shadow_offline(store, slot, workdir=workdir))
    results: list[ShadowResult] = []
    for slot in slots:
        # Snapshot active flags so a shadow pass cannot leak into the caller-visible set.
        before_active = {
            (v.skill_id, v.version): s.active for v, s, _st in store.iter_loaded()
        }
        with tempfile.TemporaryDirectory(prefix="recertia-shadow-") as tmp:
            success = evaluate(slot, Path(tmp))
        run_id = f"shadow-{slot.skill_id}-v{slot.version}-{uuid4().hex[:12]}"
        result = record_shadow_outcome(
            store, slot.skill_id, slot.version, success=success, run_id=run_id
        )
        if eval_store is not None:
            persist_shadow_observation(
                eval_store,
                slot=slot,
                success=success,
                snapshot_id=snapshot_id,
                run_id=run_id,
            )
        after_active = {
            (v.skill_id, v.version): s.active for v, s, _st in store.iter_loaded()
        }
        if after_active != before_active:
            raise RuntimeError("shadow scheduling must not alter SkillStatus.active")
        assert result.visible_to_caller is False
        results.append(result)
    return results
