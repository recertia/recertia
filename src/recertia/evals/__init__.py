"""Eval harness package: golden regression, statistics, ablation (T3), metrics store."""

from recertia.evals.golden import (
    GoldenReport,
    GoldenResult,
    run_goal_fixture,
    run_golden_for_skill,
    run_seed_library_gate,
    run_task_class_gate,
)
from recertia.evals.statistics import causal_lift, wilson_interval

__all__ = [
    "GoldenReport",
    "GoldenResult",
    "causal_lift",
    "run_goal_fixture",
    "run_golden_for_skill",
    "run_seed_library_gate",
    "run_task_class_gate",
    "wilson_interval",
]
