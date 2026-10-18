"""HTTP adapters. Handlers map RecertiaError to HTTP; services own policy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from recertia.api.deps import RouteState
from recertia.api.schemas import GoalPreview, GoalProbe, GoalSuggest
from recertia.errors import NotFound


def register_goals_routes(app: FastAPI, rs: RouteState) -> None:
    ctx = rs.ctx
    require_runs = rs.require_runs
    _optional_console_user = rs.optional_console_user
    _resolve_tenant = rs.resolve_tenant
    _require_library_write = rs.require_library_write
    _public_user = rs.public_user

    @app.post("/v1/goals/preview")
    def goals_preview(body: GoalPreview, principal=Depends(require_runs)) -> dict[str, Any]:
        return ctx.library_service.preview_goal(principal, goal=body.goal)
    @app.post("/v1/goals/suggest")
    def goals_suggest(body: GoalSuggest, principal=Depends(require_runs)) -> dict[str, Any]:
        """Pilot Compose: draft desired states (never locks criteria)."""

        return ctx.library_service.suggest_goal(
            principal,
            context=body.context,
            task_class=body.task_class or "repo-chore",
            use_model=body.use_model,
        )
    @app.post("/v1/goals/probe")
    def goals_probe(
        body: GoalProbe,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        """Read-only inventory assist for Compose / programs (never locks criteria)."""

        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        rel = body.workdir.strip().lstrip("/")
        if ".." in Path(rel).parts or Path(rel).is_absolute():
            raise HTTPException(status_code=400, detail="workdir must be relative")
        root = (ctx.root / "workspaces" / tenant_id / rel).resolve()
        try:
            root.relative_to((ctx.root / "workspaces" / tenant_id).resolve())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="workdir escapes tenant root") from exc
        return ctx.library_service.probe_goal(principal, workdir=root)
    @app.get("/v1/templates")
    def templates(principal=Depends(require_runs)) -> dict[str, Any]:
        return ctx.library_service.list_templates(principal)
    @app.get("/v1/templates/{template_id}")
    def template_detail(template_id: str, principal=Depends(require_runs)) -> dict[str, Any]:
        try:
            return ctx.library_service.get_template(principal, template_id)
        except NotFound as exc:
            raise HTTPException(status_code=404, detail=exc.message) from exc

    # ----- C0 runs list / transcript / trajectory -----

