"""HTTP/CLI composition bag. Assembled by ``create_app``; no FastAPI imports."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


@dataclass
class AppState:
    """Replaces ``ConsoleContext``. Stored on ``app.state.recertia``."""

    root: Path
    skills_root: Path
    facts_root: Path
    key_store: Any
    quota_store: Any
    runs: dict[tuple[str, str], Any]
    run_slots: Any
    record_from_state: Callable[..., Any]
    load_from_checkpoints: Callable[..., Any]
    resolve_create_workdir: Callable[..., Any]
    persist_workdir: Callable[..., Any]
    canonical_run_workdir: Callable[..., Any]
    clamp_criteria: Callable[..., Any]
    principal_may_exec: Callable[..., Any]
    require_scope: Callable[..., Any]
    validate_run_id: Callable[..., Any]
    workspace_registry: Any
    run_service: Any
    pdp: Any
    job_service: Any
    promotion_service: Any
    eval_service: Any
    library_service: Any
    program_service: Any
    workspace_service: Any
    checkpoint_reader: Any
    key_service: Any
    ops_service: Any
    sessions: Any
    proposals: Any
    programs: Any
    job_runs: Any
    events: Any
    queue: Any
    worker: Any
    embedded: Any
    model_config_for_run: Callable[..., Any]
    tenant_has_checkpoints: Callable[..., Any]
    max_criterion_timeout_s: Callable[..., Any]
    load_persisted_workdir: Callable[..., Any]
    orchestrator_factory: Callable[..., Any]
    queued_run_record: Callable[..., Any]
    enqueue_async_run: Callable[..., Any]
    resolve_task_class: Callable[..., Any]
    settings: Any = None
    principal_resolver: Any = None
    idempotency: Any = None
    _cancel_flags: set[str] = field(default_factory=set)

    @property
    def jobs(self) -> Any:
        return self.job_service

    @property
    def promotion(self) -> Any:
        return self.promotion_service

    @property
    def evals(self) -> Any:
        return self.eval_service

    @property
    def library(self) -> Any:
        return self.library_service

    @property
    def workspaces(self) -> Any:
        return self.workspace_service

    @property
    def checkpoints(self) -> Any:
        return self.checkpoint_reader

    @property
    def keys(self) -> Any:
        return self.key_service

    @property
    def ops(self) -> Any:
        return self.ops_service

    def tenant_skills_root(self, tenant_id: str) -> Path:
        from recertia.paths import tenant_skills_root

        return tenant_skills_root(
            control_root=self.root, shared=self.skills_root, tenant_id=tenant_id
        )

    def tenant_runs_root(self, tenant_id: str) -> Path:
        return self.root / "runs" / tenant_id

    def _on_async_complete(
        self,
        tenant_id: str,
        run_id: str,
        state: Any,
        *,
        mode: str = "async",
        count_usage: bool = True,
    ) -> None:
        cost = float(state.spent.cost_usd or 0.0)
        existing = self.runs.get((tenant_id, run_id))
        rec = self.record_from_state(
            run_id=run_id,
            request=state.task.request,
            task_class=state.task.task_class or "repo-chore",
            tenant_id=tenant_id,
            state=state,
            created_at=existing.created_at if existing else datetime.now(timezone.utc),
            has_goal=state.task.goal is not None,
        )
        chosen_mode = existing.mode if existing is not None and existing.mode else mode
        extras: dict[str, Any] = {"cost_usd": cost, "mode": chosen_mode}
        if existing is not None:
            if getattr(existing, "workspace_id", None):
                extras["workspace_id"] = existing.workspace_id
            if getattr(existing, "workdir", None):
                extras["workdir"] = existing.workdir
        self.runs[(tenant_id, run_id)] = rec.model_copy(update=extras)
        if not count_usage:
            return
        if self.run_service is not None:
            self.run_service.complete(tenant_id, cost_usd=cost)
        else:
            self.quota_store.complete(tenant_id, cost_usd=cost)

    def _on_async_failed(self, tenant_id: str, run_id: str, *, cancelled: bool = False) -> None:
        if self.run_service is not None:
            self.run_service.release(tenant_id)
        else:
            self.quota_store.release_inflight(tenant_id)
        existing = self.runs.get((tenant_id, run_id))
        if existing is None:
            return
        terminal = "cancelled" if cancelled else "error"
        self.runs[(tenant_id, run_id)] = existing.model_copy(
            update={"status": terminal, "terminal": terminal, "mode": "async"}
        )
