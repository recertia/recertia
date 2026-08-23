"""Budget and spend, shared by ``RunState`` and ``Branch`` (specs §10.1, §18)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=4, ge=1)
    max_tool_calls: int = Field(default=200, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    max_wall_clock_s: int = Field(default=900, ge=1)
    max_cost_usd: float | None = Field(default=None, ge=0)
    max_branches: int = Field(default=3, ge=1, le=16)
    max_parallel_steps: int = Field(default=8, ge=1)
    claim_timeout_s: int = Field(default=60, ge=1)
    max_versions_written: int = Field(default=2, ge=0)


class Spend(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempts: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    wall_clock_s: float = Field(default=0.0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    versions_written: int = Field(default=0, ge=0)


class BudgetReservation(BaseModel):
    """Resources promised to work which has not completed yet.

    Reservations make concurrent branch dispatch safe: an admission decision counts both
    recorded spend and work already admitted elsewhere.
    """

    model_config = ConfigDict(extra="forbid")

    attempts: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    wall_clock_s: float = Field(default=0.0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    versions_written: int = Field(default=0, ge=0)


def budget_excess(
    budget: Budget, spent: Spend, reservation: BudgetReservation, requested: BudgetReservation
) -> str | None:
    """Return the first exhausted dimension, counting committed and reserved resources."""

    totals = {
        "attempts": spent.attempts + reservation.attempts + requested.attempts,
        "tool_calls": spent.tool_calls + reservation.tool_calls + requested.tool_calls,
        "tokens": spent.tokens + reservation.tokens + requested.tokens,
        "wall_clock_s": spent.wall_clock_s + reservation.wall_clock_s + requested.wall_clock_s,
        "cost_usd": spent.cost_usd + reservation.cost_usd + requested.cost_usd,
        "versions_written": (
            spent.versions_written + reservation.versions_written + requested.versions_written
        ),
    }
    limits = {
        "attempts": budget.max_attempts,
        "tool_calls": budget.max_tool_calls,
        "tokens": budget.max_tokens,
        "wall_clock_s": budget.max_wall_clock_s,
        "cost_usd": budget.max_cost_usd,
        "versions_written": budget.max_versions_written,
    }
    for dimension, limit in limits.items():
        if limit is not None and totals[dimension] > limit:
            return dimension
    return None


def commit_reservation(spent: Spend, reservation: BudgetReservation) -> Spend:
    """Convert a completed reservation into spend without losing conservative estimates."""

    return spent.model_copy(
        update={
            "attempts": spent.attempts + reservation.attempts,
            "tool_calls": spent.tool_calls + reservation.tool_calls,
            "tokens": spent.tokens + reservation.tokens,
            "wall_clock_s": spent.wall_clock_s + reservation.wall_clock_s,
            "cost_usd": spent.cost_usd + reservation.cost_usd,
            "versions_written": spent.versions_written + reservation.versions_written,
        }
    )
