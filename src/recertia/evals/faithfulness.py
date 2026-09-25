"""Faithfulness harness: intervene on condensed memory and score trajectory + lift.

Eval-only T3. Production retrieve/store are not imported. Nodes and jobs must not
import this module (ADR-0005).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone

from contracts.eval import BinomialSample
from contracts.faithfulness import (
    FAITHFULNESS_STRATEGY_PREFIX,
    FaithfulnessArmResult,
    FaithfulnessIntervention,
    FaithfulnessReport,
    TrajectoryDivergence,
)
from contracts.run import MemoryBundle, RunState
from contracts.skill import SkillVersion
from recertia.evals.interventions import apply_intervention
from recertia.evals.statistics import causal_lift

_DEFAULT_INTERVENTIONS: tuple[FaithfulnessIntervention, ...] = (
    "empty",
    "corrupt",
    "irrelevant",
    "filler",
)

_EMPTY_DIVERGENCE = TrajectoryDivergence(
    jaccard=1.0,
    edit_distance=0,
    event_count_baseline=0,
    event_count_intervened=0,
    normalized_edit=0.0,
)


def strategy_tag(intervention: FaithfulnessIntervention) -> str:
    return f"{FAITHFULNESS_STRATEGY_PREFIX}{intervention}"


def event_kinds(events: Sequence[object]) -> list[str]:
    """Normalize trajectory events or raw kind strings to an event-kind sequence."""

    kinds: list[str] = []
    for event in events:
        if isinstance(event, str):
            if event:
                kinds.append(event)
            continue
        kind = getattr(event, "event_kind", None)
        if kind:
            kinds.append(str(kind))
    return kinds


def jaccard(a: Sequence[str], b: Sequence[str]) -> float:
    left, right = set(a), set(b)
    if not left and not right:
        return 1.0
    return len(left & right) / len(left | right)


def edit_distance(a: Sequence[str], b: Sequence[str]) -> int:
    """Levenshtein distance over event-kind sequences."""

    if a == b:
        return 0
    n, m = len(a), len(b)
    if n == 0:
        return m
    if m == 0:
        return n
    prev = list(range(m + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            ins = cur[j - 1] + 1
            delete = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            cur.append(min(ins, delete, sub))
        prev = cur
    return prev[-1]


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def trajectory_divergence(
    baseline: Sequence[str], intervened: Sequence[str]
) -> TrajectoryDivergence:
    edit = edit_distance(baseline, intervened)
    denom = max(len(baseline), len(intervened), 1)
    return TrajectoryDivergence(
        jaccard=jaccard(baseline, intervened),
        edit_distance=edit,
        event_count_baseline=len(baseline),
        event_count_intervened=len(intervened),
        normalized_edit=edit / denom,
    )


def pairwise_divergences(
    baseline_by_fixture: Mapping[str, Sequence[str]],
    intervened_by_fixture: Mapping[str, Sequence[str]],
) -> TrajectoryDivergence | None:
    """Median pairwise divergence for fixtures present in both maps.

    Returns None when no pair can be formed (trajectory evidence unavailable).
    """

    keys = sorted(set(baseline_by_fixture) & set(intervened_by_fixture))
    if not keys:
        return None
    jaccards: list[float] = []
    edits: list[float] = []
    norms: list[float] = []
    base_counts: list[float] = []
    int_counts: list[float] = []
    for key in keys:
        div = trajectory_divergence(list(baseline_by_fixture[key]), list(intervened_by_fixture[key]))
        jaccards.append(div.jaccard)
        edits.append(float(div.edit_distance))
        norms.append(div.normalized_edit)
        base_counts.append(float(div.event_count_baseline))
        int_counts.append(float(div.event_count_intervened))
    return TrajectoryDivergence(
        jaccard=_median(jaccards),
        edit_distance=int(round(_median(edits))),
        event_count_baseline=int(round(_median(base_counts))),
        event_count_intervened=int(round(_median(int_counts))),
        normalized_edit=_median(norms),
    )


def detectable_change(
    *,
    lift_status: str | None,
    divergence: TrajectoryDivergence | None,
    jaccard_drop: float = 0.15,
    normalized_edit_floor: float = 0.15,
) -> bool:
    if lift_status in {"established_positive", "established_negative"}:
        return True
    if divergence is None:
        return False
    if divergence.jaccard <= 1.0 - jaccard_drop:
        return True
    return divergence.normalized_edit >= normalized_edit_floor


def bundle_hook_for(
    *,
    skill_id: str,
    version: int,
    intervention: FaithfulnessIntervention,
    donor_id: str | None = None,
    donor_version: int | None = None,
):
    """Eval-only Retriever.bundle_hook. Production Retriever() never receives this."""

    def hook(bundle: MemoryBundle) -> MemoryBundle:
        if not bundle.skills:
            return bundle
        if intervention == "empty":
            return bundle
        if intervention == "irrelevant" and donor_id:
            skills = [
                (
                    candidate.model_copy(
                        update={"skill_id": donor_id, "version": donor_version or candidate.version}
                    )
                    if candidate.skill_id == skill_id and candidate.version == version
                    else candidate
                )
                for candidate in bundle.skills
            ]
            return bundle.model_copy(update={"skills": skills})
        return bundle

    return hook


class IntervenedSkillStore:
    """Eval-only overlay that replaces one skill body. Production store is never wrapped."""

    def __init__(
        self,
        inner: object,
        *,
        skill_id: str,
        version: int,
        intervention: FaithfulnessIntervention,
        donor: SkillVersion | None = None,
    ) -> None:
        self._inner = inner
        self._skill_id = skill_id
        self._version = version
        self._intervention = intervention
        self._donor = donor

    def get_version(self, skill_id: str, version: int) -> SkillVersion:
        original = self._inner.get_version(skill_id, version)  # type: ignore[attr-defined]
        if skill_id == self._skill_id and version == self._version:
            return apply_intervention(original, self._intervention, donor=self._donor)
        return original

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def evaluate_faithfulness(
    *,
    skill: SkillVersion,
    baseline: BinomialSample,
    baseline_events: Sequence[object] | None = None,
    outcomes: dict[FaithfulnessIntervention, BinomialSample],
    events: dict[FaithfulnessIntervention, Sequence[object]] | None = None,
    donor: SkillVersion | None = None,
    skill_used: bool = True,
    snapshot_id: str | None = None,
    min_independent_runs: int = 5,
    baseline_event_groups: Mapping[str, Sequence[object]] | None = None,
    event_groups: Mapping[FaithfulnessIntervention, Mapping[str, Sequence[object]]] | None = None,
) -> FaithfulnessReport:
    """Score interventions from already-collected success vectors and event streams.

    Does not run the solver. Callers that execute tasks must tag those observations
    with :func:`strategy_tag` and ``is_eval_fixture=True``. Arms with ``trials == 0``
    are listed as ``scored=False`` and do not enter the score denominator. ``donor`` is
    unused by the scorer (transformers run in the writer).
    """

    del donor
    base_groups = _coerce_groups(baseline_event_groups, baseline_events)
    arms: list[FaithfulnessArmResult] = []
    for intervention in _DEFAULT_INTERVENTIONS:
        if intervention not in outcomes:
            continue
        sample = outcomes[intervention]
        grouped = None
        if event_groups and intervention in event_groups:
            grouped = {key: event_kinds(val) for key, val in event_groups[intervention].items()}
        elif events is not None:
            grouped = _coerce_groups(None, events.get(intervention, []))
        divergence = pairwise_divergences(base_groups, grouped or {})
        if sample.trials == 0:
            arms.append(
                FaithfulnessArmResult(
                    intervention=intervention,
                    strategy=strategy_tag(intervention),
                    performance_delta=None,
                    lift=None,
                    divergence=divergence or _EMPTY_DIVERGENCE,
                    detectable_change=False,
                    skill_used=skill_used,
                    scored=False,
                )
            )
            continue
        lift = causal_lift(
            baseline,
            sample,
            task_class=skill.task_class,
            snapshot_id=snapshot_id,
            min_independent_runs=min_independent_runs,
        )
        delta = None
        if baseline.rate is not None and sample.rate is not None:
            delta = sample.rate - baseline.rate
        changed = detectable_change(lift_status=lift.status, divergence=divergence)
        if not skill_used:
            changed = False
        arms.append(
            FaithfulnessArmResult(
                intervention=intervention,
                strategy=strategy_tag(intervention),
                performance_delta=delta,
                lift=lift,
                divergence=divergence or _EMPTY_DIVERGENCE,
                detectable_change=changed,
                skill_used=skill_used,
                scored=True,
            )
        )
    scored = [arm for arm in arms if arm.scored]
    score = (sum(1 for arm in scored if arm.detectable_change) / len(scored)) if scored else None
    return FaithfulnessReport(
        skill_id=skill.skill_id,
        version=skill.version,
        task_class=skill.task_class,
        snapshot_id=snapshot_id,
        score=score,
        scored_arms=len(scored),
        arms=arms,
        baseline_successes=baseline.successes,
        baseline_trials=baseline.trials,
        at=datetime.now(timezone.utc),
    )


def _coerce_groups(
    groups: Mapping[str, Sequence[object]] | None, flat: Sequence[object] | None
) -> dict[str, list[str]]:
    if groups:
        return {key: event_kinds(val) for key, val in groups.items()}
    if flat:
        return {"_": event_kinds(flat)}
    return {}


FaithfulnessRunner = Callable[..., RunState]


def run_intervened_trials(
    *,
    skill: SkillVersion,
    intervention: FaithfulnessIntervention,
    fixture_ids: Sequence[str],
    eval_store: object,
    inner_store: object,
    donor: SkillVersion | None = None,
    runner: FaithfulnessRunner,
    snapshot_id: str = "faithfulness",
) -> list[object]:
    """Execute eval fixtures under an overlay store + retrieve hook; tag observations.

    ``runner(run_id, fixture_id, overlay=..., bundle_hook=...)`` must return a locked
    terminal ``RunState``. This module does not import ``recertia.nodes``.
    """

    del snapshot_id
    overlay = IntervenedSkillStore(
        inner_store,
        skill_id=skill.skill_id,
        version=skill.version,
        intervention=intervention,
        donor=donor,
    )
    hook = bundle_hook_for(
        skill_id=skill.skill_id,
        version=skill.version,
        intervention=intervention,
        donor_id=donor.skill_id if donor is not None else None,
        donor_version=donor.version if donor is not None else None,
    )
    recorded: list[object] = []
    for fixture_id in fixture_ids:
        run_id = f"faithfulness-{intervention}-{fixture_id}-{uuid.uuid4().hex[:8]}"
        state = runner(run_id, fixture_id, overlay=overlay, bundle_hook=hook)
        obs = eval_store.append_run(  # type: ignore[attr-defined]
            state,
            strategy_override=strategy_tag(intervention),
            force_eval_fixture=True,
        )
        recorded.append(obs)
    return recorded
