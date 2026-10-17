"""Pure Causal Skill Portfolio capacity controller (specs §24.1, ADR-0006, Phase 1).

Ranking, active-set selection, and negative-contribution retirement proposals, extracted
from :mod:`recertia.memory.procedural.active_set` as side-effect-free functions. Nothing
here touches the filesystem, a store, the clock, or the environment: the same inputs always
produce the same outputs, which is what makes the drop-in equivalence proof possible.

``PortfolioRankItem`` and ``RetirementProposal`` are internal / telemetry types. They are
**not** a second source of truth for active membership — ``SkillStatus.active`` remains the
only one (ADR-0007). Nothing in this module writes anything.

Two extension points are present but dormant in v1 (requirements FR-8, §3.7):

* ``rank_skills(..., score_fn=...)`` — a richer composite score (advantage-weighted
  influence, dependency coupling, a CoEvo-Mem-style learned residual, provenance prior).
* ``PortfolioRankItem.fidelity`` — a CrystalMem-style multi-fidelity / crystallization
  state ("liquid" / "glass" / "crystal").

Neither is used by production callers; they exist so that later elastic-memory work does
not require a breaking change to this interface.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Literal

from contracts.skill import SkillVersion
from contracts.stats import SkillStats
from contracts.status import SkillStatus
from recertia.autonomy import AutonomyConfig
from recertia.memory.procedural.retirement import retirement_decision

_NEG_INF = float("-inf")


@dataclass(frozen=True)
class PortfolioRankItem:
    """One ranked candidate. Telemetry / ordering only, never a membership record.

    Field provenance (deliberate choices, see :func:`rank_skills`):

    * ``contribution_estimate`` — ``SkillStats.contribution.estimate`` (``None`` under the
      Blind Curator nullity: no estimate without both randomized arms, specs §24.2).
    * ``interval_high`` — ``SkillStats.contribution.interval_high``. The Newcombe–Wilson
      optimistic bound; ``retirement_decision`` benches on this, not the point estimate
      (ADR-0016).
    * ``predictive_trust`` — ``SkillStats.predictive_trust.score``.
    * ``applications`` — ``SkillStats.contribution.applications``, i.e. the shadow-arm
      trial count, **not** ``predictive_trust.applications``. This is the same quantity
      ``review.lifecycle.maybe_bench_on_contribution`` compares against
      ``evidence_floor``, so :func:`propose_retirements` cannot disagree with the existing
      benching decision point about whether a skill is past the floor.
    * ``last_used_at`` — ``SkillStats.predictive_trust.last_used_at`` (may be ``None``).
    """

    skill_id: str
    version: int
    contribution_estimate: float | None
    predictive_trust: float
    applications: int
    last_used_at: datetime | None
    score: float
    # Extension point (unused in v1): future "liquid" | "glass" | "crystal" fidelity ladder.
    fidelity: str | None = None
    interval_high: float | None = None


@dataclass(frozen=True)
class RetirementProposal:
    """A proposal only. The offline plane proposes; it never writes lifecycle itself."""

    skill_id: str
    version: int
    reason: Literal["negative_contribution"]
    evidence: str | None
    contribution_estimate: float | None
    applications: int


# Optional richer score function (extension point; default is the standard key).
ScoreFn = Callable[[PortfolioRankItem], float]


def _recency_sort_key(last_used_at: datetime | None) -> tuple[int, float]:
    """Ascending surrogate for "most recently used first, never-used last".

    Returns a ``(has_value, -epoch_seconds)`` pair rather than the datetime itself so that
    ``None`` sorts last without ever being compared against a datetime, and so that a mix
    of naive and aware timestamps cannot raise. Naive values are read as UTC, which keeps
    the ordering independent of the host timezone.
    """

    if last_used_at is None:
        return (1, 0.0)
    moment = (
        last_used_at
        if last_used_at.tzinfo is not None
        else last_used_at.replace(tzinfo=timezone.utc)
    )
    return (0, -moment.timestamp())


def _sort_key(item: PortfolioRankItem) -> tuple[float, float, int, float, int, str, int]:
    """Total, deterministic order. Every component ascends, so no ``reverse=True``.

    Numeric components are negated to get a descending effect; ``skill_id`` must ascend and
    therefore could not have shared a ``reverse=True`` sort with them.
    """

    has_recency, recency = _recency_sort_key(item.last_used_at)
    return (
        -item.score,
        -item.predictive_trust,
        has_recency,
        recency,
        -item.applications,
        item.skill_id,
        item.version,
    )


def rank_skills(
    candidates: Sequence[tuple[SkillVersion, SkillStatus, SkillStats]],
    config: AutonomyConfig,
    *,
    score_fn: ScoreFn | None = None,
) -> list[PortfolioRankItem]:
    """Pure, deterministic total order over ``candidates`` (requirements FR-1, FR-2).

    Default ranking key, descending by priority:

    1. ``contribution.estimate`` if not ``None`` else ``-inf``
    2. ``predictive_trust.score``
    3. recency (``predictive_trust.last_used_at``; never-used sorts last)
    4. ``contribution.applications``
    5. ``(skill_id, version)`` ascending — a final tiebreak that makes the result
       shuffle-invariant instead of dependent on how the caller enumerated the store.
       ``version`` is compared as an integer, so v2 precedes v10.

    Skills below the evidence floor stay in the pool; a ``None`` estimate demotes a skill to
    the bottom but never drops it. Nothing is filtered out — the returned list is always the
    same length as ``candidates``.

    ``config`` is accepted for interface stability and for future score functions that need
    thresholds; the default key does not read it.

    ``score_fn`` is an extension point that is unused in v1. When supplied, items are built
    with the default ``score`` first and then re-scored, so a score function may read the
    default composite. The ordering then uses that score as component 1 and keeps the same
    tiebreak chain.
    """

    items = [
        PortfolioRankItem(
            skill_id=version.skill_id,
            version=version.version,
            contribution_estimate=stats.contribution.estimate,
            predictive_trust=stats.predictive_trust.score,
            applications=stats.contribution.applications,
            last_used_at=stats.predictive_trust.last_used_at,
            score=(
                stats.contribution.estimate
                if stats.contribution.estimate is not None
                else _NEG_INF
            ),
            interval_high=stats.contribution.interval_high,
        )
        for version, _status, stats in candidates
    ]
    if score_fn is not None:
        items = [replace(item, score=score_fn(item)) for item in items]
    items.sort(key=_sort_key)
    return items


def select_active(
    ranked: Sequence[PortfolioRankItem],
    cap: int,
) -> set[tuple[str, int]]:
    """Return the first ``cap`` ranked identities. Deterministic.

    ``cap <= 0`` yields an empty set: a non-positive cap admits nobody rather than wrapping
    around the way a negative slice bound would.
    """

    if cap <= 0:
        return set()
    return {(item.skill_id, item.version) for item in ranked[:cap]}


def propose_retirements(
    ranked: Sequence[PortfolioRankItem],
    config: AutonomyConfig,
) -> list[RetirementProposal]:
    """Negative-contribution retirement proposals only (requirement FR-3).

    A skill is proposed when, and only when::

        applications >= config.evidence_floor
        and interval_high is not None
        and interval_high < -config.retirement_threshold

    These are exactly the conditions ``review.lifecycle.maybe_bench_on_contribution``
    enforces (ADR-0016). A point estimate at or below ``-τ`` without an interval does
    not retire. ``retirement_threshold=0.0`` (``HARSH_AUTONOMY``) benches only when
    the interval is entirely negative.

    Cap-pressure benching stays outside this function: it is a capacity decision, not an
    evidence-of-harm decision, and it remains on the existing policy path.
    """

    proposals: list[RetirementProposal] = []
    for item in ranked:
        decision = retirement_decision(
            applications=item.applications,
            estimate=item.contribution_estimate,
            interval_high=item.interval_high,
            evidence_floor=config.evidence_floor,
            tau=config.retirement_threshold,
        )
        if not decision.should_retire:
            continue
        proposals.append(
            RetirementProposal(
                skill_id=item.skill_id,
                version=item.version,
                reason="negative_contribution",
                evidence=decision.evidence,
                contribution_estimate=item.contribution_estimate,
                applications=item.applications,
            )
        )
    return proposals
