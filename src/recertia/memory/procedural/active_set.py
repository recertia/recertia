"""Bounded active set per task class (specs §24.1, M5, RW-PC).

``recompute_active_set`` ranks through the pure controller in
:mod:`recertia.memory.procedural.portfolio`. Cap membership is not benching —
``propose_retirements`` is the Curator's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from contracts.eval import BinomialSample
from contracts.status import SkillStatus
from recertia.evals.store import EvalStore
from recertia.memory.procedural.contribution import (
    estimate_contribution,
    estimate_retrieval_ablation,
)
from recertia.memory.procedural.live_mix import live_mix_eligible
from recertia.memory.procedural.portfolio import rank_skills, select_active
from recertia.memory.procedural.store import SkillStore
from recertia.review.autonomy_config import DEFAULT_AUTONOMY, AutonomyConfig

if TYPE_CHECKING:
    from contracts.skill import SkillVersion
    from contracts.stats import SkillStats


def assign_active_on_approval(
    status: SkillStatus,
    *,
    version: SkillVersion | None = None,
    stats: SkillStats | None = None,
) -> SkillStatus:
    """Mark approved versions active when they may enter the live mix.

    Golden pass certifies the version. Human-authored and mined skills go active.
    ``self_distilled`` stays inactive until contribution evidence is non-negative
    (shadow slots gather that evidence). Callers that omit ``version`` keep the
    historical "approved ⇒ active" behaviour.
    """

    if status.lifecycle != "approved":
        return status.model_copy(update={"active": False})
    if version is not None and not live_mix_eligible(version, stats):
        return status.model_copy(update={"active": False})
    return status.model_copy(update={"active": True})


@dataclass(frozen=True)
class ShadowSlot:
    """An offline-only evaluation assignment, never an active retrieval slot."""

    skill_id: str
    version: int
    task_class: str
    reason: str


def select_shadow_slots(
    store: SkillStore, *, config: AutonomyConfig = DEFAULT_AUTONOMY
) -> list[ShadowSlot]:
    """Allocate bounded offline slots to benched and inactive approved versions.

    These slots do not alter ``SkillStatus.active`` and therefore cannot expand
    the caller-visible active cap.
    """

    by_class: dict[str, list[tuple]] = {}
    for version, status, stats in store.iter_loaded():
        if status.lifecycle == "benched" or (
            status.lifecycle == "approved" and not status.active
        ):
            by_class.setdefault(version.task_class, []).append((version, status, stats))
    slots: list[ShadowSlot] = []
    for task_class, rows in by_class.items():
        rows.sort(
            key=lambda row: (
                row[2].contribution.applications + row[2].contribution.suppressed_applications,
                row[0].skill_id,
                row[0].version,
            )
        )
        for version, status, _stats in rows[: config.shadow_slots_per_task_class]:
            slots.append(
                ShadowSlot(
                    skill_id=version.skill_id,
                    version=version.version,
                    task_class=task_class,
                    reason="benched" if status.lifecycle == "benched" else "newly_approved",
                )
            )
    return slots


def recompute_active_set(
    store: SkillStore,
    *,
    config: AutonomyConfig = DEFAULT_AUTONOMY,
    eval_store: EvalStore | None = None,
) -> tuple[list[SkillStatus], dict[str, float]]:
    """Cap approved actives using persisted non-judge treatment/control evidence.

    Returns ``(updated_statuses, active_cap_pressure_by_task_class)`` where pressure is
    ``max(0, approved_count - cap) / cap``.

    Ranking and the top-``cap`` cut live in :mod:`recertia.memory.procedural.portfolio`.
    This function still owns pool construction, contribution refresh, and the active-bit
    writes. It does not bench.
    """

    by_class: dict[str, list[tuple]] = {}
    for version, status, stats in store.iter_loaded():
        by_class.setdefault(version.task_class, []).append((version, status, stats))

    updated: list[SkillStatus] = []
    pressure: dict[str, float] = {}
    for task_class, rows in by_class.items():
        approved, ranked_pool, stats_by_id = _pool_for_class(
            store, rows, task_class=task_class, eval_store=eval_store
        )
        cap = config.active_cap_per_task_class
        pressure[task_class] = max(0, len(approved) - cap) / cap if cap else 0.0
        top_ids = select_active(rank_skills(ranked_pool, config), cap)
        updated.extend(
            _apply_active_bits(store, rows, top_ids=top_ids, stats_by_id=stats_by_id)
        )
    return updated, pressure


def _pool_for_class(
    store: SkillStore,
    rows: list[tuple],
    *,
    task_class: str,
    eval_store: EvalStore | None,
) -> tuple[list[tuple], list[tuple], dict[tuple[str, int], object]]:
    """Refresh contribution and split approved rows into a live-mix ranking pool.

    Returns ``(approved_with_stats, ranked_pool, stats_by_id)``. ``ranked_pool`` excludes
    ``self_distilled`` versions that have not earned live-mix admission. When an eval store
    is supplied, the pool is further narrowed to rows with a non-``None`` estimate — the
    same narrowing the pre-controller used.
    """

    empty_sample = BinomialSample(successes=0, trials=0)
    approved = [(v, s, st) for v, s, st in rows if s.lifecycle == "approved"]
    if eval_store is not None:
        retrieval_enabled, retrieval_suppressed = eval_store.retrieval_ablation_samples(
            task_class=task_class
        )
        retrieval_ablation = estimate_retrieval_ablation(
            task_class=task_class,
            retrieval_enabled=retrieval_enabled,
            retrieval_suppressed=retrieval_suppressed,
        )
        eval_store.write_retrieval_ablation(retrieval_ablation)
        samples = eval_store.contribution_samples_bulk(task_class=task_class)
        refreshed: list[tuple] = []
        for version, status, stats in approved:
            shadow, suppression = samples.get(
                (version.skill_id, version.version),
                (empty_sample, empty_sample),
            )
            contribution = estimate_contribution(
                shadow=shadow,
                suppression=suppression,
                has_required_non_judge=shadow.trials > 0 and suppression.trials > 0,
            )
            updated_stats = stats.model_copy(update={"contribution": contribution})
            if updated_stats != stats:
                store.write_stats(updated_stats)
            refreshed.append((version, status, updated_stats))
        approved = refreshed
        ranked_pool = [
            (v, s, st)
            for v, s, st in approved
            if st.contribution.estimate is not None and live_mix_eligible(v, st)
        ]
    else:
        ranked_pool = []
    if not ranked_pool:
        ranked_pool = [(v, s, st) for v, s, st in approved if live_mix_eligible(v, st)]
    stats_by_id = {(v.skill_id, v.version): st for v, _s, st in approved}
    return approved, ranked_pool, stats_by_id


def _apply_active_bits(
    store: SkillStore,
    rows: list[tuple],
    *,
    top_ids: set[tuple[str, int]],
    stats_by_id: dict[tuple[str, int], object],
) -> list[SkillStatus]:
    updated: list[SkillStatus] = []
    for version, status, stats in rows:
        current_stats = stats_by_id.get((version.skill_id, version.version), stats)
        if status.lifecycle == "approved":
            want_active = live_mix_eligible(version, current_stats) and (  # type: ignore[arg-type]
                (version.skill_id, version.version) in top_ids
            )
            new_status = status.model_copy(update={"active": want_active})
        else:
            new_status = assign_active_on_approval(
                status, version=version, stats=current_stats  # type: ignore[arg-type]
            )
        if new_status != status:
            store.write_status(new_status)
        updated.append(new_status)
    return updated


def now() -> datetime:
    return datetime.now(timezone.utc)
