"""Migration programs (Goal packs): ordered Goals with human gates (ADR-0014).

Public HTTP resource name is ``/v1/programs``. Product copy may say \"Goal pack\".
Distinct from Tower ``ReplayPack`` evidence objects.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contracts.budget import Budget
from contracts.goal import Constraint, DesiredState, Goal

DecompositionKind = Literal["by_risk", "by_layer", "by_seam", "custom"]
ProgramStatus = Literal["draft", "active", "blocked", "completed", "abandoned"]
StepStatus = Literal[
    "planned",
    "ready",
    "queued",
    "running",
    "succeeded",
    "failed",
    "skipped",
    "cancelled",
]
StepRole = Literal["characterization", "structural", "behaviour_lock", "custom"]
HandoffMode = Literal["none", "operator_workdir", "copy_forward", "git_tip"]
FreezeEnforcement = Literal["advisory", "hard"]
ProgramSource = Literal["human", "heuristic", "model", "template"]


class ExternalHandoff(BaseModel):
    """Git / PR continuity when Recertia is not the worktree owner (GP0 default)."""

    model_config = ConfigDict(extra="forbid")

    branch: str | None = None
    pr_url: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    note: str | None = None


class RepoBinding(BaseModel):
    """Allowlisted git repository for ``handoff=git_tip`` (GP2).

    ``root`` is relative to the tenant's ``repo_bindings/`` directory under the API root.
    Absolute host paths outside that tree are rejected at registration time.
    """

    model_config = ConfigDict(extra="forbid")

    binding_id: str = "default"
    root: str = Field(min_length=1, description="Relative path under tenant repo_bindings/")
    default_branch: str = "main"
    remote_url: str | None = None


class AcceptanceGate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    terminal_in: list[str] = Field(default_factory=lambda: ["solved"])
    require_program_bar: bool = True


class ProgramBudget(BaseModel):
    """Pack-level ceilings; distinct from per-run ``Budget``."""

    model_config = ConfigDict(extra="forbid")

    max_cost_usd: float | None = Field(default=None, ge=0)
    max_wall_clock_s: int | None = Field(default=None, ge=1)
    spent_cost_usd: float = Field(default=0.0, ge=0)
    spent_wall_clock_s: float = Field(default=0.0, ge=0)


class MigrationStep(BaseModel):
    """One Goal in a linear migration program."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    ordinal: int = Field(ge=0)
    title: str
    role: StepRole = "custom"
    goal: Goal
    # GP0 is linear: depends_on is reserved; runtime uses ordinal-1 only.
    depends_on: list[str] = Field(default_factory=list)
    freeze_paths: list[str] = Field(default_factory=list)
    mutate_paths: list[str] = Field(default_factory=list)
    acceptance_gate: AcceptanceGate = Field(default_factory=AcceptanceGate)
    status: StepStatus = "planned"
    run_ids: list[str] = Field(default_factory=list)
    current_run_id: str | None = None
    criteria_preview_hash: str | None = None
    external_handoff: ExternalHandoff | None = None
    skip_note: str | None = None
    goal_revision: int = Field(default=1, ge=1)


class MigrationProgram(BaseModel):
    """Durable Goal pack / migration program (tenant-scoped)."""

    model_config = ConfigDict(extra="forbid")

    program_id: str
    tenant_id: str
    title: str
    intent: str = ""
    task_class: str = "repo-chore"
    decomposition: DecompositionKind = "custom"
    status: ProgramStatus = "draft"
    steps: list[MigrationStep] = Field(default_factory=list)
    program_bar_desired: list[DesiredState] = Field(default_factory=list)
    program_bar_constraints: list[Constraint] = Field(default_factory=list)
    handoff: HandoffMode = "none"
    freeze_enforcement: FreezeEnforcement = "advisory"
    repo_binding: RepoBinding | None = None
    budget: ProgramBudget | None = None
    created_by: str = ""
    created_at: str = ""
    updated_at: str = ""
    source: ProgramSource = "human"
    disclaimer_acked_at: str | None = None

    @model_validator(mode="after")
    def _linear_ordinals_unique(self) -> "MigrationProgram":
        ordinals = [s.ordinal for s in self.steps]
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("step ordinals must be unique")
        ids = [s.step_id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step_id values must be unique within a program")
        return self


class DecompositionCandidate(BaseModel):
    """Suggest-only draft; not durable until accepted into a MigrationProgram."""

    model_config = ConfigDict(extra="forbid")

    decomposition: DecompositionKind
    rationale: str = ""
    steps: list[dict[str, Any]] = Field(default_factory=list)


def budget_from_goal_constraints(goal: Goal, base: Budget | None = None) -> Budget:
    """Apply ``budget_ceiling`` constraints onto a run Budget (closes compile_goal gap)."""

    out = (base or Budget()).model_copy(deep=True)
    for c in goal.constraints:
        if c.kind != "budget_ceiling":
            continue
        try:
            ceiling = float(c.value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"budget_ceiling {c.id!r} requires numeric value") from exc
        if out.max_cost_usd is None:
            out.max_cost_usd = ceiling
        else:
            out.max_cost_usd = min(out.max_cost_usd, ceiling)
    return out
