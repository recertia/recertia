"""Product console HTTP routes (C0–C5). Registered onto the main FastAPI app."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field

from contracts.goal import Goal
from contracts.program import ExternalHandoff
from recertia.api.console_deps import ConsoleContext, RouteState


class GoalPreview(BaseModel):
    goal: Goal


class GoalSuggest(BaseModel):
    context: str = Field(min_length=1)
    task_class: str = "repo-chore"
    use_model: bool = True


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
    max_tokens: int = 0
    task_class: str | None = None
    hint: list[str] | None = None
    arxiv_id: list[str] | None = None
    arxiv_query: str | None = None
    arxiv_max: int = 5
    with_pdf: bool = False
    pdf_sandbox: bool = False
    one_off: list[str] | None = None
    skill_id: str | None = None
    skill_version: int = 1
    fake_edge_failures: int = 0
    merge_conflicts: int = 0
    tool_upgraded: str | None = None
    edits_log: str | None = None


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


class GoalProbe(BaseModel):
    workdir: str = Field(min_length=1, description="Relative workdir under tenant workspace root")


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



def register_console_routes(app: FastAPI, ctx: ConsoleContext) -> None:
    from recertia.api.console_library_routes import register_library_routes
    from recertia.api.console_program_routes import register_program_routes
    from recertia.api.console_session_routes import register_session_routes

    rs = RouteState(ctx)
    register_session_routes(app, rs)
    register_library_routes(app, rs)
    register_program_routes(app, rs)
