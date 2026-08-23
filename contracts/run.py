"""``RunState``: the state threaded through the execution graph (specs §3).

Carries the ADR-0003-amendment and ADR-0008 fixes: ``criteria`` is typed
``list[TaskCriterion]`` (a ``SkillCertificationCriterion`` cannot type-check into it), and
``failure_signal`` — not a re-derived "some criterion failed" — is what ``classify_failure``
requires.

Variant B: ``Task.goal`` is the preferred primary input; ``request`` is optional context.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contracts.branch import BranchState, MergeAudit
from contracts.budget import Budget, BudgetReservation, Spend
from contracts.common import Arm, Strategy, Terminal
from contracts.criteria import CriterionResult, TaskCriterion
from contracts.failure import FailureSignal, FailureVerdict
from contracts.goal import Goal
from contracts.guide import ExecutionGuide
from contracts.resources import ResourceConflict


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    goal: Goal | None = Field(
        default=None,
        description="Preferred primary input (Variant B). Compiles to TaskCriterion[] at intake.",
    )
    request: str | None = Field(
        default=None,
        description=(
            "Optional natural-language context / legacy entry point. "
            "Never the sole success contract when a Goal is present."
        ),
    )
    task_class: str | None = None
    workspace: str | None = None
    submitted_by: str | None = None
    submitted_at: datetime
    is_eval_fixture: bool = Field(
        default=False,
        description=(
            "When true, distill must not author library memory (M3 firewall ahead of M4's full "
            "eval firewall). Golden / promotion runs set this so the regression gate cannot "
            "recursively learn from itself."
        ),
    )
    execution_strategy: Literal["single", "mea"] = Field(
        default="single",
        description=(
            "Runtime MEA layer. 'mea' takes effect only with policy.mea_enabled "
            "and Goal.mea_opt_in. Default 'single' is the zero-cost ordinary path."
        ),
    )

    @model_validator(mode="after")
    def _require_goal_or_request(self) -> "Task":
        has_goal = self.goal is not None
        has_request = self.request is not None and bool(self.request.strip())
        if not has_goal and not has_request:
            raise ValueError("Task requires either goal or non-empty request")
        return self


class RunManifest(BaseModel):
    """Full system fingerprint; any measurement ties to an exact state (specs §11.3)."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    model_version: str | None = None
    tool_versions: dict[str, str] = Field(default_factory=dict)
    index_snapshot_id: str | None = None
    library_commit: str | None = None
    policy_version: str | None = None
    criteria_hash: str | None = Field(
        default=None, description="sha256 of the locked TaskCriterion set's canonical serialisation."
    )
    seed: int | None = None


class MemoryElementRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plane: Literal["procedural", "semantic", "episodic", "affordance", "policy"]
    ref: str
    summary: str | None = None
    score: float | None = None
    trust: float | None = None


class SkillCandidateRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: str
    version: int
    score: float
    lexical_rank: int | None = None
    vector_rank: int | None = None
    bound_parameters: dict = Field(default_factory=dict)
    staleness_factor: float | None = None
    shadow: bool = False


class MemoryBundle(BaseModel):
    """What ``retrieve`` returns; §13.1."""

    model_config = ConfigDict(extra="forbid")

    skills: list[SkillCandidateRef] = Field(default_factory=list, max_length=3)
    facts: list[MemoryElementRef] = Field(default_factory=list, max_length=10)
    cases: list[MemoryElementRef] = Field(default_factory=list, max_length=3)
    dead_ends: list[MemoryElementRef] = Field(default_factory=list, max_length=3)
    tool_cautions: list[MemoryElementRef] = Field(default_factory=list)
    suppressed: bool = False


class StepWave(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wave: int = Field(ge=0)
    step_ids: list[str]
    attempt_no: int = 0
    snapshot_ref: str | None = None
    duration_s: float | None = None
    serial_duration_s: float | None = None


class WorkspaceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_no: int = Field(ge=0)
    snapshot_ref: str
    restored: bool = False


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["diff", "file", "json", "text", "metric_set"]
    ref: str
    description: str | None = None


class ReusabilityVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["reusable", "one_off", "duplicate"]
    nearest_skill: str | None = None
    nearest_similarity: float | None = None
    parameterisable: bool
    context_free: bool
    checkable: bool
    not_duplicate: bool
    bounded: bool
    reason: str | None = None


class RouteEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node: str
    route: str
    reason: str
    attempt_no: int = 0
    at: datetime


class RunState(BaseModel):
    """State threaded through every node (specs §3). Nodes return deltas, never a mutated copy."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    task: Task
    manifest: RunManifest = RunManifest()
    arm: Arm = "treatment"

    criteria: list[TaskCriterion] = Field(
        default_factory=list,
        description="Required set; immutable after intake. Never populated from a chosen skill.",
    )
    criteria_locked_at: datetime | None = None
    advisory_criteria: list[TaskCriterion] = Field(default_factory=list)

    bundle: MemoryBundle = MemoryBundle()
    chosen: SkillCandidateRef | None = None
    suppressed_skill: SkillCandidateRef | None = Field(
        default=None,
        description="Skill withheld for a randomized per-skill suppression comparison.",
    )
    strategy: Strategy | None = None
    strategy_reason: str | None = None
    predicted_success: float | None = Field(default=None, ge=0, le=1)

    attempt_no: int = Field(default=0, ge=0)
    branches: list[BranchState] = Field(default_factory=list, max_length=16)
    artifacts: list[Artifact] = Field(default_factory=list)
    transcript_ref: str | None = None
    workspace_snapshots: list[WorkspaceSnapshot] = Field(default_factory=list)
    step_waves: list[StepWave] = Field(default_factory=list)
    resource_conflicts: list[ResourceConflict] = Field(default_factory=list)

    results: list[CriterionResult] = Field(default_factory=list)
    results_history: list[list[CriterionResult]] = Field(default_factory=list)
    certification_observations: list[CriterionResult] = Field(
        default_factory=list,
        description=(
            "Advisory: the applied skill's certification criteria scored against this run's "
            "artifact. Feeds SkillStats/needs_recert only; never gates routing or the caller's "
            "result (ADR-0003 amendment)."
        ),
    )
    merge_audits: list[MergeAudit] = Field(default_factory=list)
    failure_signal: FailureSignal | None = None
    failure: FailureVerdict | None = None

    draft: dict | None = None
    facts_extracted: list[dict] = Field(default_factory=list)
    affordance_updates: list[dict] = Field(default_factory=list)
    reusability: ReusabilityVerdict | None = None
    written_versions: list[dict] = Field(default_factory=list)
    execution_guide: ExecutionGuide | None = None

    budget: Budget = Budget()
    spent: Spend = Spend()
    reserved: BudgetReservation = BudgetReservation()
    route_log: list[RouteEntry] = Field(default_factory=list)
    terminal: Terminal | None = None
    mea_active: bool = Field(
        default=False,
        description="True when three-layer MEA activation succeeded at intake.",
    )
    mea_fallback_reason: str | None = Field(
        default=None,
        description="Set when MEA was requested but any activation layer was missing.",
    )

    @model_validator(mode="after")
    def _control_arm_suppresses_bundle(self) -> "RunState":
        if self.arm == "control" and not self.bundle.suppressed and (
            self.bundle.skills or self.bundle.facts or self.bundle.cases
        ):
            raise ValueError("arm='control' MUST return an empty, suppressed bundle (specs §5)")
        return self

    @model_validator(mode="after")
    def _suppression_requires_control_arm(self) -> "RunState":
        if self.suppressed_skill is not None and self.arm != "control":
            raise ValueError("suppressed_skill is only valid for arm='control'")
        return self

    @model_validator(mode="after")
    def _decomposition_criteria_partition(self) -> "RunState":
        if not any(b.kind == "decomposition" for b in self.branches):
            return self
        owned: list[str] = []
        for b in self.branches:
            if b.kind == "decomposition":
                owned.extend(b.owned_criteria)
        if len(owned) != len(set(owned)):
            raise ValueError("decomposition branches must own disjoint criteria sets (specs §18)")
        # Criteria that can only be scored on the merged artifact are legitimately left unowned
        # by any branch (specs §18); this validator only checks disjointness among owned ids.
        return self
