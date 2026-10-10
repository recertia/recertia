"""Fan-out branches and merge audits (specs §18, §26.4).

Includes the refactor-plan's S3 fix: ``Branch`` previously had no status, spend, transcript, or
snapshot reference, and the schema omitted ``budget`` entirely. ``BranchState`` here is
complete enough for ``evolve`` to restore a branch and for a merge audit to attribute a missing
input to something concrete.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from contracts.budget import Budget, BudgetReservation, Spend
from contracts.criteria import CriterionResult
from contracts.resources import ResourceClaim

BranchKind = Literal["portfolio", "decomposition"]
BranchStrategy = Literal["apply", "adapt", "scratch"]


class BranchState(BaseModel):
    """One dispatched unit of a fan-out (specs §18)."""

    model_config = ConfigDict(extra="forbid")

    branch_id: str
    kind: BranchKind = "portfolio"
    strategy: BranchStrategy
    subtask: str | None = Field(
        default=None, description="decomposition only: the part of the work this branch owns."
    )
    candidate: dict[str, Any] | None = None
    workspace_ref: str
    snapshot_ref: str | None = None
    transcript_ref: str | None = None
    status: Literal["dispatched", "running", "succeeded", "failed", "timed_out"] = "dispatched"
    resources: list[ResourceClaim] = Field(default_factory=list)
    budget: Budget
    spent: Spend = Spend()
    reserved: BudgetReservation = BudgetReservation()
    results: list[CriterionResult] = Field(default_factory=list)
    selected: bool = False
    margin: float | None = Field(default=None, description="Winner score minus runner-up.")
    cost_usd: float | None = None
    owned_criteria: list[str] = Field(
        default_factory=list,
        description=(
            "decomposition only: ids of the run's TaskCriterion this branch is accountable for. "
            "Every locked criterion MUST be owned by exactly one branch or retained at the join "
            "(specs §18)."
        ),
    )


class MergeAudit(BaseModel):
    """Every fan-in records expected against received inputs (specs §26.4)."""

    model_config = ConfigDict(extra="forbid")

    merge_id: str
    expected: int = Field(ge=0)
    received: int = Field(ge=0)
    missing: list[str] = Field(default_factory=list)
    action: Literal["proceeded", "flagged", "failed"]
    layered: bool = False
    batches: list[list[str]] = Field(
        default_factory=list,
        description="Actual fan-in batches in execution order; empty for a direct merge.",
    )

    @property
    def is_complete(self) -> bool:
        """The real predicate the routing table needed; ``MergeAudit`` never had ``.complete``."""

        return self.received >= self.expected and not self.missing
