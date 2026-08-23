"""Trajectory event stream (ADR-0011): decision-level log per run.

Structural definition only. Append/storage live in ``src/recertia/trajectory/``.
Events are emitted by the graph engine from node outcomes — nodes themselves never write
the trajectory store (same separation as the memory ledger).

``audited_state_delta`` was added for the MEA / AuditedTaskState projection
(see contracts/audited_task_state.py and the 2026-08-23 plan).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EventKind = Literal[
    "criteria_locked",
    "retrieval_result",
    "plan_choice",
    "step_started",
    "step_finished",
    "artifact_produced",
    "criterion_scored",
    "failure_classified",
    "evolve_decision",
    "distill_candidate",
    "merge_audit",
    "terminal",
    "late_signal",
    "audited_state_delta",  # MEA: accepted controller-owned state projection update
]

ArmName = Literal["treatment", "control", "shadow", "practice"]


class TrajectoryEvent(BaseModel):
    """One decision-boundary event within a run (ADR-0011)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    seq: int = Field(ge=0)
    node: str
    attempt_no: int = Field(ge=0)
    event_kind: EventKind
    at: datetime

    memory_snapshot_id: str | None = None
    library_commit: str | None = None
    criteria_hash: str | None = None
    model_ref: str | None = None
    policy_version: str | None = None

    summary: str | None = None
    payload_ref: str | None = None
    payload_inline: dict | None = None

    skill_id: str | None = None
    skill_version: int | None = Field(default=None, ge=1)
    criterion_id: str | None = None
    branch_id: str | None = None


class Trajectory(BaseModel):
    """Full trajectory for one run (header + ordered events)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    task_id: str
    task_class: str | None = None
    arm: ArmName = "treatment"
    is_eval_fixture: bool = False
    events: list[TrajectoryEvent] = Field(default_factory=list)
    closed: bool = False
    closed_at: datetime | None = None
