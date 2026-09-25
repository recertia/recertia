"""Binomial Wilson intervals, causal lift, and calibration scoring (specs §19, §23)."""

from __future__ import annotations

import math
from typing import Iterable, Sequence

from contracts.eval import (
    BinomialSample,
    CausalLiftResult,
    ConfidenceInterval,
    LiftStatus,
    RunVariance,
)


def _z_for(level: float) -> float:
    # Common levels without pulling in SciPy.
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
    # Newcombe (1998) method 10: combine Wilson bounds asymmetrically.
    low = diff - math.sqrt((treatment.rate - ti.low) ** 2 + (ci.high - control.rate) ** 2)
    high = diff + math.sqrt((ti.high - treatment.rate) ** 2 + (control.rate - ci.low) ** 2)
    return ConfidenceInterval(low=low, high=high, level=level, method="newcombe_wilson")


def run_variance(rates: Sequence[float]) -> RunVariance:
    """Sample std-dev, best, worst, and absolute best–worst gap over independent rates."""

    vals = [float(r) for r in rates]
    n = len(vals)
    if n == 0:
        return RunVariance(n_runs=0)
    best = max(vals)
    worst = min(vals)
    gap = abs(best - worst)
    if n == 1:
        return RunVariance(
            n_runs=1,
            std_dev=0.0,
            best_rate=best,
            worst_rate=worst,
            best_worst_gap=gap,
        )
    mean_rate = sum(vals) / n
    var = sum((x - mean_rate) ** 2 for x in vals) / (n - 1)
    return RunVariance(
        n_runs=n,
        std_dev=math.sqrt(var),
        best_rate=best,
        worst_rate=worst,
        best_worst_gap=gap,
    )


def bernoulli_rates(sample: BinomialSample) -> list[float]:
    """Reproducible 0/1 vector from a binomial sample (successes first)."""

    if sample.trials == 0:
        return []
    return [1.0] * sample.successes + [0.0] * (sample.trials - sample.successes)


def classify_lift(estimate: float | None, interval: ConfidenceInterval | None) -> LiftStatus:
    if estimate is None or interval is None:
        return "insufficient_data"
    if interval.low > 0:
        return "established_positive"
    if interval.high < 0:
        return "established_negative"
    return "not_established"


def causal_lift(
    treatment: BinomialSample,
    control: BinomialSample,
    *,
    task_class: str = "repo-chore",
    level: float = 0.95,
    snapshot_id: str | None = None,
    model_version: str | None = None,
    window: str | None = None,
    min_independent_runs: int = 5,
    treatment_rates: Sequence[float] | None = None,
    control_rates: Sequence[float] | None = None,
    paired_lifts: Sequence[float] | None = None,
) -> CausalLiftResult:
    """Compute treatment − control first-attempt success with status language (specs §19).

    ``independent_runs`` is the observation/trial count (min of the two arms), not the
    snapshot count, so a 100-trial window still establishes lift. Below
    ``min_independent_runs`` an otherwise-established interval is reported as
    ``low_run_count``.

    ``paired_lifts`` is the per-snapshot (treatment − control) series aligned on
    snapshot id. When omitted, list-zip of ``treatment_rates`` / ``control_rates`` is
    kept for callers that already pass aligned series.
    """

    independent_runs = min(treatment.trials, control.trials)
    t_rates = list(treatment_rates) if treatment_rates is not None else bernoulli_rates(treatment)
    c_rates = list(control_rates) if control_rates is not None else bernoulli_rates(control)
    t_var = run_variance(t_rates) if t_rates else None
    c_var = run_variance(c_rates) if c_rates else None
    lift_var = None
    if paired_lifts is not None:
        if paired_lifts:
            lift_var = run_variance([float(v) for v in paired_lifts])
    elif treatment_rates is not None and control_rates is not None:
        paired = min(len(treatment_rates), len(control_rates))
        if paired:
            lifts = [float(treatment_rates[i]) - float(control_rates[i]) for i in range(paired)]
            lift_var = run_variance(lifts)

    if treatment.trials == 0 or control.trials == 0:
        return CausalLiftResult(
            task_class=task_class,
            treatment=treatment,
            control=control,
            estimate=None,
            interval=None,
            status="insufficient_data",
            snapshot_id=snapshot_id,
            model_version=model_version,
            window=window,
            treatment_variance=t_var,
            control_variance=c_var,
            lift_variance=lift_var,
            min_independent_runs=min_independent_runs,
            independent_runs=independent_runs,
        )
    assert treatment.rate is not None and control.rate is not None
    estimate = treatment.rate - control.rate
    interval = newcombe_wilson_difference(treatment, control, level=level)
    status = classify_lift(estimate, interval)
    if status in ("established_positive", "established_negative") and independent_runs < min_independent_runs:
        status = "low_run_count"
    return CausalLiftResult(
        task_class=task_class,
        treatment=treatment,
        control=control,
        estimate=estimate,
        interval=interval,
        status=status,
        snapshot_id=snapshot_id,
        model_version=model_version,
        window=window,
        treatment_variance=t_var,
        control_variance=c_var,
        lift_variance=lift_var,
        min_independent_runs=min_independent_runs,
        independent_runs=independent_runs,
    )


def brier_score(predictions: Sequence[float], outcomes: Sequence[bool]) -> float:
    """Calibration error as Brier score of ``predicted_success`` (specs §23)."""

    if not predictions:
        raise ValueError("brier_score requires at least one observation")
    if len(predictions) != len(outcomes):
        raise ValueError("predictions and outcomes must be the same length")
    total = 0.0
    for pred, outcome in zip(predictions, outcomes, strict=True):
        y = 1.0 if outcome else 0.0
        total += (pred - y) ** 2
    return total / len(predictions)


def rate(successes: int, trials: int) -> float | None:
    if trials == 0:
        return None
    return successes / trials


def mean(values: Iterable[float]) -> float | None:
    vals = list(values)
    if not vals:
        return None
    return sum(vals) / len(vals)
