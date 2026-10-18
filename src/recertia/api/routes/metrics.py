"""HTTP adapters. Handlers map RecertiaError to HTTP; services own policy."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Header, Request

from recertia.api.deps import RouteState
from recertia.evals.store import EvalStore
from recertia.memory.procedural.store import SkillStore


def register_metrics_routes(app: FastAPI, rs: RouteState) -> None:
    ctx = rs.ctx
    require_runs = rs.require_runs
    require_metrics = rs.require_metrics
    _optional_console_user = rs.optional_console_user
    _resolve_tenant = rs.resolve_tenant
    _require_library_write = rs.require_library_write
    _public_user = rs.public_user

    @app.get("/v1/metrics/dashboard")
    def dashboard(
        principal=Depends(require_metrics),
    ) -> dict[str, Any]:
        return ctx.eval_service.metrics_dashboard(
            principal,
            tenant_id=principal.tenant_id,
            quota_snapshot=ctx.quota_store.snapshot(principal.tenant_id),
        )

    @app.get("/v1/metrics/report")
    def metrics_report(
        request: Request,
        principal=Depends(require_metrics),
        task_class: str = "repo-chore",
        snapshot_id: str | None = None,
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        eval_db = ctx.tenant_runs_root(tenant_id) / "evals.db"
        store = EvalStore(eval_db)
        try:
            skill_store = SkillStore(ctx.tenant_skills_root(tenant_id))
            report = ctx.eval_service.metrics_report(
                principal,
                eval_store=store,
                skill_store=skill_store,
                task_class=task_class,
                snapshot_id=snapshot_id,
                tenant_id=tenant_id,
            )
        finally:
            store.close()
        return report.model_dump(mode="json")
    @app.get("/v1/metrics/canary")
    def metrics_canary(principal=Depends(require_metrics)) -> dict[str, Any]:
        report = ctx.eval_service.run_canary(principal)
        return {
            "trials": report.trials,
            "false_passes": report.false_passes,
            "false_pass_rate": report.false_pass_rate,
            "model_version": report.model_version,
        }
    @app.get("/v1/console/tower-summary")
    def tower_summary(
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        """C4: practice conversion + active cap pressure for Tower panels."""

        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        skill_store = SkillStore(ctx.tenant_skills_root(tenant_id))
        eval_db = ctx.tenant_runs_root(tenant_id) / "evals.db"
        eval_store = EvalStore(eval_db) if eval_db.exists() else None
        try:
            return ctx.library_service.tower_summary(
                principal,
                skill_store=skill_store,
                eval_store=eval_store,
                proposals=ctx.proposals,
                tenant_id=tenant_id,
            )
        finally:
            if eval_store is not None:
                eval_store.close()

