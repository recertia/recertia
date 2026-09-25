"""Single retirement predicate (ADR-0006, ADR-0016). Two adapters, one decision.

``propose_retirements`` is the pure adapter (no I/O).
``maybe_bench_on_contribution`` is the effectful adapter (writes lifecycle).
``recompute_active_set`` must not call this — cap membership is not benching.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RetirementAction = Literal["below_floor", "no_estimate", "no_interval", "keep", "retire"]


@dataclass(frozen=True)
class RetirementDecision:
    """What the bench predicate concluded. Never a write."""

    action: RetirementAction
    estimate: float | None
    applications: int
    interval_high: float | None = None
    evidence: str | None = None

    @property
    def should_retire(self) -> bool:
        return self.action == "retire"


def retirement_decision(
    *,
    applications: int,
    estimate: float | None,
    interval_high: float | None,
    evidence_floor: int,
    tau: float,
) -> RetirementDecision:
    """Bench when, and only when, evidence is past the floor and ``interval_high < -tau``.

    A missing estimate or a missing interval is not harm (ADR-0016). The bound is
    strict: ``interval_high == -tau`` keeps the skill. ``tau == 0.0`` therefore
    benches only when the interval is entirely negative.
    """

    if applications < evidence_floor:
        return RetirementDecision(
            action="below_floor",
            estimate=estimate,
            applications=applications,
            interval_high=interval_high,
        )
    if estimate is None:
        return RetirementDecision(
            action="no_estimate",
            estimate=None,
            applications=applications,
            interval_high=interval_high,
        )
    if interval_high is None:
        return RetirementDecision(
            action="no_interval",
            estimate=estimate,
            applications=applications,
            interval_high=None,
        )
    if interval_high >= -tau:
        return RetirementDecision(
            action="keep",
            estimate=estimate,
            applications=applications,
            interval_high=interval_high,
            evidence=f"interval_high={interval_high}",
        )
    return RetirementDecision(
        action="retire",
        estimate=estimate,
        applications=applications,
        interval_high=interval_high,
        evidence=f"interval_high={interval_high}",
    )
