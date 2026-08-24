"""Console HTTP context and per-request helpers (extracted from the registrar)."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from recertia.api.console_auth import ConsoleUser, SessionStore
from recertia.api.events import RunEventLog
from recertia.api.jobs_store import JobRunStore
from recertia.api.quotas import QuotaStore
from recertia.programs.store import ProgramStore
from recertia.proposals.store import ProposalStore
from recertia.workers.run_worker import AsyncRunWorker


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
        self.sessions = SessionStore()
        self.proposals = ProposalStore(root / "proposals.sqlite")
        self.programs = ProgramStore(root / "programs.sqlite")
        self.job_runs = JobRunStore(root / "job_runs.sqlite")
        self._program_idempotency: dict[str, str] = {}
        self.events = RunEventLog(root / "run_events")
        self.worker = AsyncRunWorker(
            events=self.events,
            on_complete=self._on_async_complete,
            on_failed=self._on_async_failed,
        )
        self._cancel_flags: set[str] = set()

    def tenant_skills_root(self, tenant_id: str) -> Path:
        if os.environ.get("RECERTIA_TENANT_SKILLS", "").strip() in {"1", "true", "yes"}:
            path = self.root / "tenants" / tenant_id / "skills"
            path.mkdir(parents=True, exist_ok=True)
            return path
        return self.skills_root

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
        self.quota_store.complete(tenant_id, cost_usd=cost)

    def _on_async_failed(self, tenant_id: str, run_id: str, *, cancelled: bool = False) -> None:
        self.quota_store.release_inflight(tenant_id)
        existing = self.runs.get((tenant_id, run_id))
        if existing is None:
            return
        terminal = "cancelled" if cancelled else "error"
        self.runs[(tenant_id, run_id)] = existing.model_copy(
            update={"status": terminal, "terminal": terminal, "mode": "async"}
        )



class RouteState:
    """Shared closures for split console route modules."""

    def __init__(self, ctx: ConsoleContext) -> None:
        self.ctx = ctx
        self.require_runs = ctx.require_scope("runs", ctx.key_store)
        self.require_metrics = ctx.require_scope("metrics", ctx.key_store)

    def optional_console_user(self, request: Request) -> ConsoleUser | None:
        token = request.headers.get("X-Recertia-Session") or request.cookies.get(
            "recertia_session"
        )
        return self.ctx.sessions.parse(token)

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

    def require_workspace_admin(self, request: Request, principal: Any) -> str:
        """Admin API key or console role admin may mutate the registry."""

        user = self.optional_console_user(request)
        if user is not None and user.may("admin"):
            return user.user_id
        if "admin" in principal.scopes:
            return principal.key_id
        raise HTTPException(status_code=403, detail="admin required to register workspace")

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
