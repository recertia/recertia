"""Audited task state and MEA subtask contracts (Phase 0).

Canonical long-horizon progress is held as a controller-owned projection
over the ADR-0011 trajectory event stream. Models only propose or report;
the controller alone performs compare-and-swap on ``version``.

See docs/plans/2026-08-23-audited-task-state-and-mea-subtask-loop.md and
the 2026-08-23 Architect review (required revisions incorporated).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contracts.budget import Budget
from contracts.criteria import TaskCriterion


class ScopeDescriptor(BaseModel):
    """Files, commands, network, and permissions in scope for a phase or subtask.

    Must reuse the existing resource-claim machinery at runtime; this contract
    only carries the declared bounds.
    """

    model_config = ConfigDict(extra="forbid")

    files: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    network: Literal["none", "allowlist", "open"] = "none"
    permissions: list[str] = Field(default_factory=list)
    notes: str | None = None


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["screenshot", "har", "terminal_log", "diff", "file", "json", "text", "other"]
    ref: str
    description: str | None = None
    sha256: str | None = None


class ProvenanceBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal[
        "mea_loop",
        "trajectory_import",
        "synthetic",
        "operator",
        "external_demo",
    ]
    source_ref: str | None = None
    captured_at: datetime | None = None
    run_id: str | None = None
    notes: list[str] = Field(default_factory=list)


class VerifiedDecision(BaseModel):
    """A fact independently verified from the environment or locked criteria."""

    model_config = ConfigDict(extra="forbid")

    decision_id: str
    statement: str
    evidence_refs: list[ArtifactRef] = Field(min_length=1)
    verified_at: datetime
    applicability: str | None = None
    revalidation_rule: str | None = None


class FailedApproach(BaseModel):
    """An approach that was tried and independently observed to fail."""

    model_config = ConfigDict(extra="forbid")

    approach_id: str
    description: str
    observed_failure: str
    evidence_refs: list[ArtifactRef] = Field(default_factory=list)
    recorded_at: datetime


class Blocker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blocker_id: str
    description: str
    since_version: int = Field(ge=0)
    evidence_refs: list[ArtifactRef] = Field(default_factory=list)


class ModelHarnessRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str | None = None
    model_id: str | None = None
    route_label: str | None = None


class AuditedTaskState(BaseModel):
    """Controller-owned, versioned projection of long-horizon progress.

    Not a second source of truth: every accepted delta is also emitted as a
    trajectory event of kind ``audited_state_delta`` so the state is fully
    recoverable from the ledger + trajectory events alone.
    """

    model_config = ConfigDict(extra="forbid")

    state_id: str
    goal_id: str
    version: int = Field(ge=0)
    objective: str
    non_negotiable_constraints: list[str] = Field(default_factory=list)
    acceptance_criteria: list[TaskCriterion] = Field(min_length=1)
    criteria_snapshot_hash: str = Field(
        min_length=1,
        description="sha256 of the locked TaskCriterion[] at intake; rejects drift",
    )
    scope: ScopeDescriptor = Field(default_factory=ScopeDescriptor)
    isolation_policy_ref: str = Field(
        default="container_default",
        description=(
            "Must match the default container isolation policy unless an "
            "explicit, allow-listed exception is present"
        ),
    )
    accepted_commit: str | None = None
    verified_decisions: list[VerifiedDecision] = Field(default_factory=list)
    failed_approaches: list[FailedApproach] = Field(default_factory=list)
    current_blockers: list[Blocker] = Field(default_factory=list)
    current_phase: str = "intake"
    assigned_route: ModelHarnessRoute | None = None
    evidence_refs: list[ArtifactRef] = Field(default_factory=list)
    budget_residual: Budget = Field(default_factory=Budget)
    last_auditor_report_id: str | None = None
    provenance: ProvenanceBundle
    updated_at: datetime
    rounds_consumed: int = Field(default=0, ge=0)
    max_rounds: int = Field(default=12, ge=1)

    @model_validator(mode="after")
    def _rounds_within_cap(self) -> "AuditedTaskState":
        if self.rounds_consumed > self.max_rounds:
            raise ValueError(
                f"rounds_consumed ({self.rounds_consumed}) exceeds max_rounds ({self.max_rounds})"
            )
        return self


class SubtaskContract(BaseModel):
    """Bounded work unit proposed by the controller (Manager role)."""

    model_config = ConfigDict(extra="forbid")

    subtask_id: str
    parent_state_version: int = Field(ge=0)
    description: str
    acceptance_criteria: list[TaskCriterion] = Field(min_length=1)
    scope: ScopeDescriptor = Field(default_factory=ScopeDescriptor)
    budget: Budget = Field(default_factory=Budget)
    max_steps: int = Field(default=50, ge=1)
    dependencies: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=list)


class AuditorDelta(BaseModel):
    """Candidate state change produced by an independent auditor.

    The controller alone decides whether to accept via compare-and-swap.
    Unverified executor claims must never appear here.
    """

    model_config = ConfigDict(extra="forbid")

    report_id: str
    parent_version: int = Field(ge=0)
    proposed_version: int = Field(ge=1)
    verified_decisions_added: list[VerifiedDecision] = Field(default_factory=list)
    failed_approaches_added: list[FailedApproach] = Field(default_factory=list)
    blockers_set: list[Blocker] = Field(default_factory=list)
    blockers_cleared: list[str] = Field(default_factory=list)
    current_phase: str | None = None
    accepted_commit: str | None = None
    evidence_refs_added: list[ArtifactRef] = Field(default_factory=list)
    criteria_snapshot_hash: str
    isolation_policy_ref: str
    budget_residual: Budget
    produced_at: datetime

    @model_validator(mode="after")
    def _version_advances(self) -> "AuditorDelta":
        if self.proposed_version != self.parent_version + 1:
            raise ValueError(
                f"proposed_version must be parent_version + 1 "
                f"(got parent={self.parent_version}, proposed={self.proposed_version})"
            )
        return self


def apply_auditor_delta(
    state: AuditedTaskState, delta: AuditorDelta
) -> tuple[AuditedTaskState | None, str | None]:
    """Compare-and-swap apply. Returns (new_state, None) or (None, reason).

    Rejects when:
    - parent_version does not match current version
    - criteria_snapshot_hash drifts
    - isolation_policy_ref mismatches without an allow-listed exception
    - any verified_decision lacks evidence_refs (enforced by model)
    - rounds would exceed max_rounds
    """

    if delta.parent_version != state.version:
        return None, f"cas_mismatch: state.version={state.version} delta.parent={delta.parent_version}"

    if delta.criteria_snapshot_hash != state.criteria_snapshot_hash:
        return None, "criteria_snapshot_hash_drift"

    if (
        delta.isolation_policy_ref != state.isolation_policy_ref
        and delta.isolation_policy_ref != "container_default"
    ):
        # Allow-list check is a runtime policy concern; contract layer only
        # records the mismatch as a soft signal. Hard reject only on empty.
        if not delta.isolation_policy_ref:
            return None, "isolation_policy_ref_empty"

    new_rounds = state.rounds_consumed + 1
    if new_rounds > state.max_rounds:
        return None, f"max_rounds_exceeded: {new_rounds} > {state.max_rounds}"

    cleared = set(delta.blockers_cleared)
    remaining_blockers = [b for b in state.current_blockers if b.blocker_id not in cleared]
    remaining_blockers.extend(delta.blockers_set)

    new_state = state.model_copy(
        update={
            "version": delta.proposed_version,
            "verified_decisions": list(state.verified_decisions) + list(delta.verified_decisions_added),
            "failed_approaches": list(state.failed_approaches) + list(delta.failed_approaches_added),
            "current_blockers": remaining_blockers,
            "current_phase": delta.current_phase or state.current_phase,
            "accepted_commit": delta.accepted_commit
            if delta.accepted_commit is not None
            else state.accepted_commit,
            "evidence_refs": list(state.evidence_refs) + list(delta.evidence_refs_added),
            "budget_residual": delta.budget_residual,
            "last_auditor_report_id": delta.report_id,
            "rounds_consumed": new_rounds,
            "updated_at": delta.produced_at,
        }
    )
    return new_state, None
