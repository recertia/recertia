"""M4 unit tests: Wilson / causal_lift / calibration / ablation / run variance."""

from __future__ import annotations

import pytest

from contracts.eval import BinomialSample
from recertia.evals.ablation import assign_arm
from recertia.evals.statistics import brier_score, causal_lift, run_variance, wilson_interval


def test_injected_lift_excludes_zero() -> None:
    result = causal_lift(
        BinomialSample(successes=80, trials=100),
        BinomialSample(successes=50, trials=100),
        task_class="repo-chore",
    )
    assert result.estimate == pytest.approx(0.30)
    assert result.interval is not None
    assert result.interval.low > 0
    assert result.status == "established_positive"
    assert result.render_status() == "established positive"
    assert result.independent_runs == 100
    # Pin the Newcombe/Wilson construction numerically (method stability).
    assert result.interval.low == pytest.approx(0.169084, abs=1e-5)
    assert result.interval.high == pytest.approx(0.416997, abs=1e-5)


def test_null_effect_reports_not_established() -> None:
    result = causal_lift(
        BinomialSample(successes=50, trials=100),
        BinomialSample(successes=50, trials=100),
        task_class="repo-chore",
    )
    assert result.estimate == pytest.approx(0.0)
    assert result.interval is not None
    assert result.interval.low <= 0 <= result.interval.high
    assert result.status == "not_established"
    assert result.render_status() == "not established"


def test_zero_sample_arm_is_insufficient_data() -> None:
    result = causal_lift(
        BinomialSample(successes=10, trials=20),
        BinomialSample(successes=0, trials=0),
    )
    assert result.estimate is None
    assert result.status == "insufficient_data"


def test_few_trials_cannot_claim_established_lift() -> None:
    result = causal_lift(
        BinomialSample(successes=4, trials=4),
        BinomialSample(successes=0, trials=4),
        min_independent_runs=5,
    )
    assert result.interval is not None
    assert result.interval.low > 0
    assert result.independent_runs == 4
    assert result.status == "low_run_count"
    assert result.render_status() == "low run count"


def test_run_variance_known_gap() -> None:
    variance = run_variance([0.2, 0.4, 0.6])
    assert variance.n_runs == 3
    assert variance.best_rate == pytest.approx(0.6)
    assert variance.worst_rate == pytest.approx(0.2)
    assert variance.best_worst_gap == pytest.approx(0.4)
    assert variance.std_dev == pytest.approx(0.2)


def test_snapshot_rates_drive_best_worst_gap() -> None:
    result = causal_lift(
        BinomialSample(successes=40, trials=50),
        BinomialSample(successes=25, trials=50),
        treatment_rates=[0.8, 0.9, 0.7, 0.85, 0.75],
        control_rates=[0.5, 0.55, 0.45, 0.5, 0.48],
        min_independent_runs=5,
    )
    assert result.status == "established_positive"
    assert result.treatment_variance is not None
    assert result.treatment_variance.n_runs == 5
    assert result.treatment_variance.best_worst_gap == pytest.approx(0.2)
    assert result.lift_variance is not None
    assert result.lift_variance.n_runs == 5
    assert result.lift_variance.best_rate is not None
    assert result.lift_variance.worst_rate is not None
    assert result.lift_variance.best_worst_gap == pytest.approx(
        result.lift_variance.best_rate - result.lift_variance.worst_rate
    )


def test_bernoulli_vector_variance_is_reproducible() -> None:
    result = causal_lift(
        BinomialSample(successes=80, trials=100),
        BinomialSample(successes=50, trials=100),
    )
    assert result.treatment_variance is not None
    assert result.treatment_variance.n_runs == 100
    assert result.treatment_variance.best_rate == 1.0
    assert result.treatment_variance.worst_rate == 0.0
    assert result.treatment_variance.best_worst_gap == 1.0
    recomputed = run_variance([1.0] * 80 + [0.0] * 20)
    assert result.treatment_variance.std_dev == pytest.approx(recomputed.std_dev)


def test_wilson_interval_bounds() -> None:
    interval = wilson_interval(50, 100)
    assert interval is not None
    assert 0 <= interval.low < 0.5 < interval.high <= 1


def test_brier_score_perfect_and_worst() -> None:
    assert brier_score([1.0, 0.0], [True, False]) == pytest.approx(0.0)
    assert brier_score([0.0, 1.0], [True, False]) == pytest.approx(1.0)


def test_ablation_exclusions_and_determinism() -> None:
    excluded = assign_arm(run_id="r1", task_class="repo-chore", is_eval_fixture=True)
    assert excluded.eligible is False
    assert excluded.arm == "treatment"

    a = assign_arm(run_id="same", task_class="repo-chore", seed=7, rate=0.5)
    b = assign_arm(run_id="same", task_class="repo-chore", seed=7, rate=0.5)
    assert a.arm == b.arm
    assert a.reason == b.reason
