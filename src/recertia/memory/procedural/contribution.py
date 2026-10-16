"""Per-skill contribution from shadow vs suppression (specs §24.2, M5).

Class-level retrieval lift lives on ``RetrievalAblationEffect``; this module estimates
one skill's effect from randomized shadow/suppression samples — not by subtracting a
task-class control baseline from a selected skill (refactor-plan S4).
"""

from __future__ import annotations

from datetime import datetime, timezone

from contracts.eval import BinomialSample
from contracts.stats import Contribution, RetrievalAblationEffect
from recertia.stats import newcombe_wilson_difference


def estimate_contribution(
    *,
    shadow: BinomialSample,
    suppression: BinomialSample,
    has_required_non_judge: bool,
) -> Contribution:
    """Estimate one skill's effect from randomized shadow/suppression samples.

    ``has_required_non_judge`` is required: a judge-only sample pair is not a
    contribution input (specs §24.2 / references.md §1.8). Callers must compute
    the flag from the observation set, not inherit a default.
    """

    if not has_required_non_judge:
        # Preserve the shadow counts for audit; drop the suppression arm so
        # ``Contribution.estimate`` is the Blind Curator ``None``.
        return Contribution(
            applications=shadow.trials,
            successes=shadow.successes,
            last_evaluated_at=datetime.now(timezone.utc),
        )
    if shadow.trials == 0 or suppression.trials == 0:
        return Contribution(
            applications=shadow.trials,
            successes=shadow.successes,
            suppressed_applications=suppression.trials,
            suppressed_successes=suppression.successes,
            last_evaluated_at=datetime.now(timezone.utc),
        )
    interval = newcombe_wilson_difference(
        shadow,
        suppression,
    )
    return Contribution(
        applications=shadow.trials,
        successes=shadow.successes,
        suppressed_applications=suppression.trials,
        suppressed_successes=suppression.successes,
        interval_low=interval.low if interval else None,
        interval_high=interval.high if interval else None,
        last_evaluated_at=datetime.now(timezone.utc),
    )


def estimate_retrieval_ablation(
    *,
    task_class: str,
    retrieval_enabled: BinomialSample,
    retrieval_suppressed: BinomialSample,
) -> RetrievalAblationEffect:
    """Estimate class-level retrieval availability from randomized arm assignments."""

    interval = newcombe_wilson_difference(retrieval_enabled, retrieval_suppressed)
    return RetrievalAblationEffect(
        task_class=task_class,
        retrieval_enabled=retrieval_enabled.trials,
        retrieval_enabled_successes=retrieval_enabled.successes,
        retrieval_suppressed=retrieval_suppressed.trials,
        retrieval_suppressed_successes=retrieval_suppressed.successes,
        interval_low=interval.low if interval else None,
        interval_high=interval.high if interval else None,
        last_evaluated_at=datetime.now(timezone.utc),
    )


def trust_score(*, applications: int, successes: int) -> float:
    return (successes + 1) / (applications + 2)
