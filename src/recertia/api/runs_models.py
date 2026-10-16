"""Request and response models for ``/v1/runs``."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, model_validator

from contracts.common import Arm
from contracts.goal import Goal


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
    mode: str = "sync"  # sync | async (console C2)
    model: str | None = None  # console-selected provider:slug; omitted → process env

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
