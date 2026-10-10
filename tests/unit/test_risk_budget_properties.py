"""Property sweeps for contracts.budget admission (hand-rolled; repo has no hypothesis)."""

from __future__ import annotations

import itertools

import pytest

from contracts.budget import (
    Budget,
    BudgetExhaustedError,
    BudgetReservation,
    ResidualBudget,
    Spend,
    budget_excess,
    commit_reservation,
)

DIMS = {  # reservation field -> Budget limit field
    "attempts": "max_attempts",
    "tool_calls": "max_tool_calls",
    "tokens": "max_tokens",
    "wall_clock_s": "max_wall_clock_s",
    "cost_usd": "max_cost_usd",
    "versions_written": "max_versions_written",
}
BUDGET = Budget(
    max_attempts=4,
    max_tool_calls=10,
    max_tokens=100,
    max_wall_clock_s=60,
    max_cost_usd=1.0,
    max_versions_written=2,
)


def _bump(limit: float) -> float:
    return 0.01 if isinstance(limit, float) else 1


@pytest.mark.parametrize("dim,limit_field", DIMS.items())
def test_exact_limit_admits_one_over_rejects(dim: str, limit_field: str) -> None:
    limit = getattr(BUDGET, limit_field)
    at = BudgetReservation(**{dim: limit})
    assert budget_excess(BUDGET, Spend(), BudgetReservation(), at) is None
    over = BudgetReservation(**{dim: limit + _bump(limit)})
    assert budget_excess(BUDGET, Spend(), BudgetReservation(), over) == dim


@pytest.mark.parametrize("dim,limit_field", DIMS.items())
def test_spent_reserved_requested_all_count(dim: str, limit_field: str) -> None:
    limit = getattr(BUDGET, limit_field)
    part = limit / 2 if isinstance(limit, float) else limit // 2
    spent = Spend(**{dim: part})
    reserved = BudgetReservation(**{dim: part})
    rest = limit - 2 * part
    assert budget_excess(BUDGET, spent, reserved, BudgetReservation(**{dim: rest})) is None
    over = BudgetReservation(**{dim: rest + _bump(limit)})
    assert budget_excess(BUDGET, spent, reserved, over) == dim


def test_monotone_and_commit_conserves() -> None:
    for a, t, v in itertools.product(range(6), range(0, 13, 3), range(4)):
        req = BudgetReservation(attempts=a, tool_calls=t, versions_written=v)
        verdict = budget_excess(BUDGET, Spend(), BudgetReservation(), req)
        if verdict is None:  # anything smaller must also be admitted
            for a2, v2 in itertools.product(range(a + 1), range(v + 1)):
                smaller = BudgetReservation(attempts=a2, tool_calls=t, versions_written=v2)
                assert budget_excess(BUDGET, Spend(), BudgetReservation(), smaller) is None
        committed = commit_reservation(Spend(), req)
        assert budget_excess(BUDGET, committed, BudgetReservation(), BudgetReservation()) == verdict


def test_none_limits_are_unbounded_not_zero() -> None:
    b = Budget(max_tokens=None, max_cost_usd=None)
    huge = Spend(tokens=10**9, cost_usd=1e6)
    assert budget_excess(b, huge, BudgetReservation(), BudgetReservation()) is None


def test_exhausted_residual_attempts_do_not_readmit() -> None:
    """Bug 6 (CTO_REVIEW §4): exhausted attempts must fail closed, not clamp to 1."""
    with pytest.raises(BudgetExhaustedError):
        ResidualBudget(max_attempts=0).as_admission_budget()


def test_positive_residual_attempts_pass_through() -> None:
    for n in range(1, 6):
        assert ResidualBudget(max_attempts=n).as_admission_budget().max_attempts == n
