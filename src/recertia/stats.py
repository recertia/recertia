"""Proportion intervals with no eval-package imports."""

from __future__ import annotations

import math

from contracts.eval import BinomialSample, ConfidenceInterval


def _z_for(level: float) -> float:
    table = {0.90: 1.6448536269514722, 0.95: 1.959963984540054, 0.99: 2.5758293035489004}
    if level not in table:
        raise ValueError(f"unsupported confidence level {level}; use one of {sorted(table)}")
    return table[level]


def wilson_interval(
    successes: int, trials: int, *, level: float = 0.95
) -> ConfidenceInterval | None:
    """Single-proportion Wilson score interval."""

    if trials <= 0:
        return None
    z = _z_for(level)
    p = successes / trials
    z2 = z * z
    denom = 1.0 + z2 / trials
    centre = (p + z2 / (2 * trials)) / denom
    half = (z / denom) * math.sqrt(p * (1.0 - p) / trials + z2 / (4 * trials * trials))
    return ConfidenceInterval(
        low=max(0.0, centre - half),
        high=min(1.0, centre + half),
        level=level,
        method="wilson",
    )


def newcombe_wilson_difference(
    treatment: BinomialSample,
    control: BinomialSample,
    *,
    level: float = 0.95,
) -> ConfidenceInterval | None:
    """Newcombe score interval for a difference of proportions (Wilson per arm)."""

    ti = wilson_interval(treatment.successes, treatment.trials, level=level)
    ci = wilson_interval(control.successes, control.trials, level=level)
    if ti is None or ci is None:
        return None
    assert treatment.rate is not None and control.rate is not None
    diff = treatment.rate - control.rate
    low = diff - math.sqrt((treatment.rate - ti.low) ** 2 + (ci.high - control.rate) ** 2)
    high = diff + math.sqrt((ti.high - treatment.rate) ** 2 + (control.rate - ci.low) ** 2)
    return ConfidenceInterval(low=low, high=high, level=level, method="newcombe_wilson")
