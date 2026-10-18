"""HTTP request/response models. Routers import these; services take schemas or dicts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from contracts.common import Arm
from contracts.goal import Goal
from contracts.program import ExternalHandoff


class RunCreate(BaseModel):
    goal: Goal | None = None
    request: str | None = None
    task_class: str | None = None
    criteria: list[dict[str, Any]] | None = None
    script: list[str] | None = None
    budget: dict[str, Any] | None = None
    workdir: str | None = None
    workspace_id: str | None = None
    run_id: str | None = None
    arm: Arm = "treatment"
    mode: str = "sync"
    model: str | None = None

    @model_validator(mode="after")
    def _require_goal_or_request(self) -> "RunCreate":
        if self.goal is None and (self.request is None or not self.request.strip()):
            raise ValueError("Provide goal or non-empty request")
        if self.mode not in {"sync", "async"}:
            raise ValueError("mode must be sync or async")
        return self


class RunRecord(BaseModel):
    run_id: str
    request: str | None = None
    task_class: str
    status: str
    created_at: datetime
    tenant_id: str
    terminal: str | None = None
    failure_class: str | None = None
    attempt_no: int | None = None
    arm: str | None = None
    route_log: list[dict[str, Any]] | None = None
    has_goal: bool = False
    cost_usd: float | None = None
    mode: str | None = None
    criteria_hash: str | None = None
    workspace_id: str | None = None
    workdir: str | None = None


class GoalPreview(BaseModel):
    goal: Goal


class GoalSuggest(BaseModel):
    context: str = Field(min_length=1)
    task_class: str = "repo-chore"
    use_model: bool = True


class GoalProbe(BaseModel):
    workdir: str = Field(min_length=1, description="Relative workdir under tenant workspace root")


class DevLogin(BaseModel):
    user_id: str = "dev-operator"
    display_name: str = "Dev Operator"
    roles: list[str] = Field(default_factory=lambda: ["operator"])
    tenants: list[str] = Field(default_factory=lambda: ["default"])
    active_tenant: str | None = None


class ProposalDecision(BaseModel):
    decision: Literal["approve", "reject", "request_changes"]
    note: str = ""


class JobTrigger(BaseModel):
    dry_run: bool = True
    max_proposals: int = 10
    hint: list[str] | None = None
    one_off: list[str] | None = None
    skill_id: str | None = None
    skill_version: int = 1
    fake_edge_failures: int = 0
    merge_conflicts: int = 0
    tool_upgraded: str | None = None


class TenantSwitch(BaseModel):
    tenant_id: str


class ProgramCreate(BaseModel):
    title: str
    intent: str = ""
    task_class: str = "repo-chore"
    decomposition: Literal["by_risk", "by_layer", "by_seam", "custom"] = "custom"
    handoff: Literal["none", "operator_workdir", "copy_forward", "git_tip"] = "none"
    freeze_enforcement: Literal["advisory", "hard"] = "advisory"
    steps: list[dict[str, Any]] = Field(default_factory=list)
    program_bar_desired: list[dict[str, Any]] = Field(default_factory=list)
    program_bar_constraints: list[dict[str, Any]] = Field(default_factory=list)
    source: Literal["human", "heuristic", "model", "template"] = "human"


class ProgramFromPack(BaseModel):
    title: str
    intent: str = ""
    task_class: str = "repo-chore"
    decomposition: Literal["by_risk", "by_layer", "by_seam", "custom"] = "by_risk"
    steps: list[dict[str, Any]] = Field(min_length=1)


class ProgramAccept(BaseModel):
    ack_disclaimer: bool = True


class RepoBindingBody(BaseModel):
    root: str = Field(min_length=1)
    binding_id: str = "default"
    default_branch: str = "main"
    remote_url: str | None = None


class RecordTipBody(BaseModel):
    """Record HEAD from a path under tenant workspaces or the binding root."""

    workdir: str | None = None
    use_binding_root: bool = False


class SeedWorkdirBody(BaseModel):
    run_id: str
    tip_sha: str | None = None


class StepPatch(BaseModel):
    title: str | None = None
    goal: Goal | None = None
    freeze_paths: list[str] | None = None
    mutate_paths: list[str] | None = None
    role: Literal["characterization", "structural", "behaviour_lock", "custom"] | None = None
    external_handoff: ExternalHandoff | None = None


class StepSkipBody(BaseModel):
    note: str


class StepRunBody(BaseModel):
    plan_only: bool = False
    workdir: str | None = None
    workspace_id: str | None = None
    budget: dict[str, Any] | None = None
    bind_run_id: str | None = None
    idempotency_key: str | None = None


class WorkspaceCreate(BaseModel):
    workspace_id: str
    display_name: str
    host_root: str
    notes: str | None = None


class WorkspacePatch(BaseModel):
    display_name: str | None = None
    notes: str | None = None
    enabled: bool | None = None
    clear_notes: bool = False


class EvalRunBody(BaseModel):
    task_class: str = "repo-chore"
    snapshot: str | None = None
    golden_dir: str | None = None
    golden_root: str | None = None


class PolicyProposalBody(BaseModel):
    policy_diff: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""


class ReviewDecisionBody(BaseModel):
    decision: str
    note: str = ""


class DistillTrajectoryBody(BaseModel):
    task_class: str
    trajectory: dict[str, Any]


class MemoryQueryBody(BaseModel):
    query: str = Field(min_length=1)
    workdir: str | None = None
    limit: int = Field(default=8, ge=1, le=50)
    env: dict[str, str] | None = None
