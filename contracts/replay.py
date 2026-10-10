"""Counterfactual replay contracts (ADR-0011).

Replay runs offline against stored trajectories under a candidate WorldState.
Solver nodes must never import this module (boundary test).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from contracts.budget import Budget
from contracts.eval import ConfidenceInterval, LiftStatus

ReplayMode = Literal["retrieval_only", "validate_only", "full_execution"]

ReplayObsStatus = Literal["completed", "skipped", "failed"]


class WorldState(BaseModel):
    """Candidate library / policy world for counterfactual evaluation."""

    model_config = ConfigDict(extra="forbid")

    library_commit: str | None = None
    index_snapshot_id: str | None = None
    skill_status_overrides: dict[str, str] = Field(
        default_factory=dict,
        description='Map "skill_id@version" -> lifecycle override (e.g. retired).',
    )
    suppressed_skill_ids: list[str] = Field(
        default_factory=list,
        description="Skill ids removed from retrieval candidates for this world.",
    )
    policy_version: str | None = None
    model_ref: str | None = None
    tool_versions: dict[str, str] = Field(default_factory=dict)


class ReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trajectory_ref: str = Field(description="run_id or trajectory store key")
    mode: ReplayMode
    world: WorldState
    budget: Budget | None = None
    sample_seed: int | None = None


class ReplayObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    mode: ReplayMode
    original_first_attempt_success: bool | None = None
    counterfactual_first_attempt_success: bool | None = None
    original_skill_id: str | None = None
    counterfactual_skill_id: str | None = None
    original_bundle_summary: dict[str, Any] | None = None
    counterfactual_bundle_summary: dict[str, Any] | None = None
    plan_would_change: bool | None = None
    criterion_deltas: list[dict[str, Any]] = Field(default_factory=list)
    status: ReplayObsStatus = "completed"
    reason: str | None = None
    child_run_id: str | None = None
    at: datetime


class ReplayPack(BaseModel):
    """Aggregate counterfactual pack attached to Curator / recert evidence."""

    model_config = ConfigDict(extra="forbid")

    pack_id: str
    purpose: str
    world: WorldState
    mode: ReplayMode
    observations: list[ReplayObservation] = Field(default_factory=list)
    treatment_successes: int = Field(ge=0, default=0)
    treatment_trials: int = Field(ge=0, default=0)
    counterfactual_successes: int = Field(ge=0, default=0)
    counterfactual_trials: int = Field(ge=0, default=0)
    estimate: float | None = None
    interval: ConfidenceInterval | None = None
    status: LiftStatus = "insufficient_data"
    created_at: datetime
