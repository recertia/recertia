"""The bench predicate is one function; adapters must agree with it."""

from __future__ import annotations

from recertia.memory.procedural.portfolio import propose_retirements
from recertia.memory.procedural.retirement import retirement_decision
from recertia.review.autonomy_config import DEFAULT_AUTONOMY, HARSH_AUTONOMY, AutonomyConfig
from tests.unit.memory.test_portfolio import _item


def test_below_floor_never_retires() -> None:
    decision = retirement_decision(
        applications=DEFAULT_AUTONOMY.evidence_floor - 1,
        estimate=-0.9,
        interval_high=-0.9,
        evidence_floor=DEFAULT_AUTONOMY.evidence_floor,
        tau=DEFAULT_AUTONOMY.retirement_threshold,
    )
    assert decision.action == "below_floor"
    assert not decision.should_retire


def test_null_estimate_never_retires() -> None:
    decision = retirement_decision(
        applications=10_000,
        estimate=None,
        interval_high=-0.9,
        evidence_floor=DEFAULT_AUTONOMY.evidence_floor,
        tau=DEFAULT_AUTONOMY.retirement_threshold,
    )
    assert decision.action == "no_estimate"
    assert not decision.should_retire


def test_missing_interval_never_retires() -> None:
    decision = retirement_decision(
        applications=DEFAULT_AUTONOMY.evidence_floor,
        estimate=-0.9,
        interval_high=None,
        evidence_floor=DEFAULT_AUTONOMY.evidence_floor,
        tau=DEFAULT_AUTONOMY.retirement_threshold,
    )
    assert decision.action == "no_interval"
    assert not decision.should_retire


def test_interval_at_minus_tau_keeps() -> None:
    tau = DEFAULT_AUTONOMY.retirement_threshold
    decision = retirement_decision(
        applications=DEFAULT_AUTONOMY.evidence_floor,
        estimate=-tau,
        interval_high=-tau,
        evidence_floor=DEFAULT_AUTONOMY.evidence_floor,
        tau=tau,
    )
    assert decision.action == "keep"
    assert not decision.should_retire


def test_interval_strictly_below_minus_tau_retires() -> None:
    tau = DEFAULT_AUTONOMY.retirement_threshold
    high = -tau - 0.01
    decision = retirement_decision(
        applications=DEFAULT_AUTONOMY.evidence_floor,
        estimate=-0.4,
        interval_high=high,
        evidence_floor=DEFAULT_AUTONOMY.evidence_floor,
        tau=tau,
    )
    assert decision.should_retire
    assert decision.evidence == f"interval_high={high}"


def test_adapters_agree_on_the_grid() -> None:
    configs = [DEFAULT_AUTONOMY, HARSH_AUTONOMY, AutonomyConfig(evidence_floor=0)]
    estimates: list[float | None] = [None, -1.0, -0.25, -0.05, 0.0, 0.05, 0.5]
    highs: list[float | None] = [None, -1.0, -0.25, -0.05, 0.0, 0.05, 0.5]
    for config in configs:
        for applications in (0, 1, config.evidence_floor - 1, config.evidence_floor, 500):
            if applications < 0:
                continue
            for estimate in estimates:
                for interval_high in highs:
                    decision = retirement_decision(
                        applications=applications,
                        estimate=estimate,
                        interval_high=interval_high,
                        evidence_floor=config.evidence_floor,
                        tau=config.retirement_threshold,
                    )
                    proposed = propose_retirements(
                        [
                            _item(
                                "grid",
                                estimate=estimate,
                                applications=applications,
                                interval_high=interval_high,
                            )
                        ],
                        config,
                    )
                    assert bool(proposed) is decision.should_retire
