"""Console HTTP context and per-request helpers (extracted from the registrar)."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from recertia.api.console_auth import ConsoleUser, SessionStore, console_session
from recertia.api.events import RunEventLog
from recertia.api.jobs_store import JobRunStore
from recertia.api.quotas import QuotaStore
from recertia.ports.identity import ledger_actor
from recertia.programs.store import ProgramStore
from recertia.proposals.store import ProposalStore


class ConsoleContext:
    def __init__(
        self,
        *,
        root: Path,
        skills_root: Path,
        facts_root: Path,
        key_store: Any,
        quota_store: QuotaStore,
        runs: dict,
        run_slots: threading.Semaphore,
        record_from_state: Any,
        load_from_checkpoints: Any,
        resolve_create_workdir: Any,
        persist_workdir: Any,
        canonical_run_workdir: Any,
        clamp_criteria: Any,
        principal_may_exec: Any,
        require_scope: Any,
        validate_run_id: Any,
        workspace_registry: Any = None,
        run_service: Any = None,
        pdp: Any = None,
    ) -> None:
        self.root = root
        self.skills_root = skills_root
        self.facts_root = facts_root
        self.key_store = key_store
        self.quota_store = quota_store
        self.runs = runs
        self.run_slots = run_slots
        self.record_from_state = record_from_state
        self.load_from_checkpoints = load_from_checkpoints
        self.resolve_create_workdir = resolve_create_workdir
        self.persist_workdir = persist_workdir
        self.canonical_run_workdir = canonical_run_workdir
        self.clamp_criteria = clamp_criteria
        self.principal_may_exec = principal_may_exec
        self.require_scope = require_scope
        self.validate_run_id = validate_run_id
        self.workspace_registry = workspace_registry
        self.run_service = run_service
        if pdp is None:
            from recertia.app.authz import PolicyDecisionPoint

            pdp = PolicyDecisionPoint.load(surface="api")
        self.pdp = pdp
        from recertia.app.jobs import JobService

        self.job_service = JobService(pdp=pdp)
        self.sessions = SessionStore(path=root / "console_sessions.sqlite")
        self.proposals = ProposalStore(root / "proposals.sqlite")
        self.programs = ProgramStore(root / "programs.sqlite")
        self.job_runs = JobRunStore(root / "job_runs.sqlite")
        self._program_idempotency: dict[str, str] = {}
        from recertia.workers.embedded import EmbeddedQueueWorker
        from recertia.workers.queue import open_run_queue
        from recertia.workers.run_worker import AsyncRunWorker

        self.events = RunEventLog(root)
        self.queue = open_run_queue(root)
        self.queue.reconcile_orphans()
        self.worker = AsyncRunWorker(
            events=self.events,
            on_complete=self._on_async_complete,
            on_failed=self._on_async_failed,
        )
        self.worker.attach_queue(self.queue)
        self.embedded = EmbeddedQueueWorker(self.worker, self.queue)
        self._cancel_flags: set[str] = set()

    def tenant_skills_root(self, tenant_id: str) -> Path:
        from recertia.paths import tenant_skills_root

        return tenant_skills_root(
            control_root=self.root, shared=self.skills_root, tenant_id=tenant_id
        )

    def tenant_runs_root(self, tenant_id: str) -> Path:
        return self.root / "runs" / tenant_id

    def _on_async_complete(self, tenant_id: str, run_id: str, state: Any) -> None:
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
        self.runs[(tenant_id, run_id)] = rec.model_copy(
            update={"cost_usd": cost, "mode": "async"}
        )
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

    def model_config_for_run(self, model: str | None) -> Any:
        from recertia.api import _model_config_for_run

        return _model_config_for_run(model)

    def tenant_has_checkpoints(self, tenant_id: str, run_id: str) -> bool:
        from recertia.api import _tenant_has_checkpoints

        return _tenant_has_checkpoints(self.root, tenant_id, run_id)

    def max_criterion_timeout_s(self) -> int:
        from recertia.api import _api_max_criterion_timeout_s

        return _api_max_criterion_timeout_s()

    def load_persisted_workdir(self, tenant_id: str, run_id: str) -> Path | None:
        from recertia.api import _load_persisted_workdir

        return _load_persisted_workdir(
            self.root, tenant_id, run_id, registry=self.workspace_registry
        )

    def resolve_task_class(
        self, *, explicit: str | None, goal_task_class: str | None
    ) -> str:
        from recertia.bootstrap import resolve_task_class

        return resolve_task_class(explicit=explicit, goal_task_class=goal_task_class)

    def orchestrator_factory(self) -> Any:
        """Resolve at call time so tests can patch ``recertia.api.build_default_orchestrator``."""

        import recertia.api as api_mod

        return api_mod.build_default_orchestrator

    def queued_run_record(self, **kwargs: Any) -> Any:
        from recertia.api.runs_models import RunRecord

        return RunRecord(**kwargs)

    def enqueue_async_run(self, **kwargs: Any) -> None:
        from recertia.workers.queue import QueueItem
        from recertia.workers.run_worker import AsyncRunRequest, dump_async_request

        queued = AsyncRunRequest(**kwargs)
        self.queue.enqueue(
            QueueItem(
                tenant_id=queued.tenant_id,
                run_id=queued.run_id,
                kind="run",
                payload=dump_async_request(queued),
            )
        )


class RouteState:
    """Shared closures for split console route modules."""

    def __init__(self, ctx: ConsoleContext) -> None:
        self.ctx = ctx
        self.require_runs = ctx.require_scope("runs", ctx.key_store)
        self.require_metrics = ctx.require_scope("metrics", ctx.key_store)

    def optional_console_user(self, request: Request) -> ConsoleUser | None:
        return console_session(self.ctx.sessions, request)

    def resolve_tenant(
        self,
        principal: Any,
        request: Request,
        x_recertia_tenant: str | None,
    ) -> str:
        """API keys are single-tenant; console sessions may switch among memberships (C5)."""

        user = self.optional_console_user(request)
        if x_recertia_tenant:
            if user is not None:
                if x_recertia_tenant not in user.tenants:
                    raise HTTPException(status_code=403, detail="tenant not in membership")
                return x_recertia_tenant
            if x_recertia_tenant != principal.tenant_id:
                raise HTTPException(status_code=403, detail="api key tenant mismatch")
            return x_recertia_tenant
        if user is not None:
            return user.active_tenant
        return principal.tenant_id

    def require_host_admin(self, request: Request, principal: Any) -> str:
        """Binding a host directory requires platform_admin, not tenant admin."""

        user = self.optional_console_user(request)
        roles = user.roles if user is not None else None
        if self.ctx.pdp.may_admin_host(principal, console_roles=roles):
            return user.user_id if user is not None else ledger_actor(principal)
        raise HTTPException(
            status_code=403, detail="platform_admin required to register a host root"
        )

    def require_workspace_admin(self, request: Request, principal: Any) -> str:
        """Tenant admin may patch or disable a workspace. Host registration is separate."""

        user = self.optional_console_user(request)
        console_is_admin = user.may("admin") if user is not None else None
        if self.ctx.pdp.may_admin_registry(principal, console_is_admin=console_is_admin):
            return user.user_id if user is not None else ledger_actor(principal)
        raise HTTPException(status_code=403, detail="admin required to update workspace")

    def require_library_write(
        self, request: Request, principal: Any, *, scope: str, min_role: str = "reviewer"
    ) -> None:
        """Promote / jobs: console reviewer, or API key with dedicated scope / admin."""

        user = self.optional_console_user(request)
        if user is not None:
            if not user.may(min_role):  # type: ignore[arg-type]
                raise HTTPException(status_code=403, detail=f"requires role {min_role}")
            return
        if "admin" in principal.scopes or scope in principal.scopes:
            return
        raise HTTPException(status_code=403, detail=f"missing scope: {scope}")

    def public_user(self, user: ConsoleUser) -> dict[str, Any]:
        return {
            "user_id": user.user_id,
            "display_name": user.display_name,
            "roles": sorted(user.roles),
            "tenants": list(user.tenants),
            "active_tenant": user.active_tenant,
        }
