"""HTTP adapters. Handlers map RecertiaError to HTTP; services own policy."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from recertia.api.deps import RouteState
from recertia.api.schemas import WorkspaceCreate, WorkspacePatch
from recertia.errors import ConfigError, Conflict, Forbidden, NotFound
from recertia.ports.identity import ledger_actor


def register_workspaces_routes(app: FastAPI, rs: RouteState) -> None:
    ctx = rs.ctx
    require_runs = rs.require_runs
    _optional_console_user = rs.optional_console_user
    _resolve_tenant = rs.resolve_tenant
    _require_library_write = rs.require_library_write
    _public_user = rs.public_user

    @app.get("/v1/workspaces")
    def list_workspaces(
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        if ctx.workspace_registry is None:
            raise HTTPException(status_code=500, detail="workspace registry unavailable")
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        items = ctx.workspace_service.list(
            principal, registry=ctx.workspace_registry, tenant_id=tenant_id
        )
        return {"workspaces": [w.model_dump(mode="json") for w in items]}
    @app.get("/v1/workspaces/{workspace_id}")
    def get_workspace(
        workspace_id: str,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        if ctx.workspace_registry is None:
            raise HTTPException(status_code=500, detail="workspace registry unavailable")
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        try:
            ws = ctx.workspace_service.get(
                principal,
                registry=ctx.workspace_registry,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
            )
        except NotFound as exc:
            raise HTTPException(status_code=404, detail=exc.message) from exc
        return ws.model_dump(mode="json")
    @app.post("/v1/workspaces", status_code=201)
    def create_workspace(
        body: WorkspaceCreate,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        if ctx.workspace_registry is None:
            raise HTTPException(status_code=500, detail="workspace registry unavailable")
        user = _optional_console_user(request)
        actor = user.user_id if user is not None else ledger_actor(principal)
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        try:
            ws = ctx.workspace_service.register(
                principal,
                registry=ctx.workspace_registry,
                tenant_id=tenant_id,
                workspace_id=body.workspace_id,
                display_name=body.display_name,
                host_root=body.host_root,
                created_by=actor,
                notes=body.notes,
                console_roles=frozenset(user.roles) if user is not None else None,
            )
        except Forbidden as exc:
            raise HTTPException(status_code=403, detail=exc.message) from exc
        except Conflict as exc:
            raise HTTPException(status_code=409, detail=exc.message) from exc
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc
        return ws.model_dump(mode="json")
    @app.patch("/v1/workspaces/{workspace_id}")
    def patch_workspace(
        workspace_id: str,
        body: WorkspacePatch,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        if ctx.workspace_registry is None:
            raise HTTPException(status_code=500, detail="workspace registry unavailable")
        user = _optional_console_user(request)
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        try:
            ws = ctx.workspace_service.update(
                principal,
                registry=ctx.workspace_registry,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                display_name=body.display_name,
                notes=body.notes,
                enabled=body.enabled,
                clear_notes=body.clear_notes,
                console_is_admin=user.may("admin") if user is not None else None,
            )
        except Forbidden as exc:
            raise HTTPException(status_code=403, detail=exc.message) from exc
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc
        except NotFound as exc:
            raise HTTPException(status_code=404, detail=exc.message) from exc
        return ws.model_dump(mode="json")
    @app.delete("/v1/workspaces/{workspace_id}")
    def delete_workspace(
        workspace_id: str,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        if ctx.workspace_registry is None:
            raise HTTPException(status_code=500, detail="workspace registry unavailable")
        user = _optional_console_user(request)
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        try:
            ws = ctx.workspace_service.delete(
                principal,
                registry=ctx.workspace_registry,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                console_is_admin=user.may("admin") if user is not None else None,
            )
        except Forbidden as exc:
            raise HTTPException(status_code=403, detail=exc.message) from exc
        except NotFound as exc:
            raise HTTPException(status_code=404, detail=exc.message) from exc
        return ws.model_dump(mode="json")

