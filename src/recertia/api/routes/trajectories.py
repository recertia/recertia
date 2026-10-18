"""HTTP adapters. Handlers map RecertiaError to HTTP; services own policy."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Header, Request

from recertia.api.console_auth import console_session
from recertia.api.deps import RouteState
from recertia.api.errors import V1HTTPError
from recertia.api.schemas import DistillTrajectoryBody
from recertia.errors import ConfigError


def _tenant(ctx, principal, request, x_recertia_tenant):
    user = console_session(ctx.sessions, request)
    if x_recertia_tenant:
        if user is not None:
            if x_recertia_tenant not in user.tenants:
                raise V1HTTPError(403, code="tenant_forbidden", message="tenant not in membership")
            return x_recertia_tenant
        if x_recertia_tenant != principal.tenant_id:
            raise V1HTTPError(403, code="tenant_mismatch", message="api key tenant mismatch")
        return x_recertia_tenant
    if user is not None:
        return user.active_tenant
    return str(principal.tenant_id)


def register_trajectories_routes(app: FastAPI, rs: RouteState) -> None:
    ctx = rs.ctx
    require_runs = rs.require_runs
    _optional_console_user = rs.optional_console_user
    _resolve_tenant = rs.resolve_tenant
    _require_library_write = rs.require_library_write
    _public_user = rs.public_user

    @app.post("/v1/trajectories/import")
    def import_trajectory(
        body: dict[str, Any],
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _tenant(ctx, principal, request, x_recertia_tenant)
        try:
            return ctx.library_service.import_trajectory(
                principal,
                body=body,
                runs_root=ctx.root,
                tenant_id=tenant_id,
                actor=getattr(principal, "key_id", "api"),
            )
        except ConfigError as exc:
            raise V1HTTPError(400, code="import_rejected", message=exc.message) from exc
    @app.post("/v1/trajectories/distill")
    def distill_trajectory(
        body: DistillTrajectoryBody,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        from recertia.memory.procedural.store import SkillStore

        tenant_id = _tenant(ctx, principal, request, x_recertia_tenant)
        store = SkillStore(ctx.tenant_skills_root(tenant_id))
        try:
            return ctx.library_service.distill_trajectory(
                principal,
                store=store,
                trajectory=body.trajectory,
                actor=getattr(principal, "key_id", "api"),
                task_class=body.task_class,
            )
        except ConfigError as exc:
            raise V1HTTPError(400, code="distill_rejected", message=exc.message) from exc

