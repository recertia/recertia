"""Console session routes: auth, OIDC, registered workspaces."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response

from recertia.api.console_auth import (
    ConsoleUser,
    auth_mode,
    dev_admin_enabled,
    dev_login_enabled,
    oidc_authorize_url,
    oidc_configured,
    oidc_exchange_code,
    pkce_challenge,
    session_cookie_kwargs,
)
from recertia.api.console_deps import RouteState
from recertia.api.console_routes import (
    DevLogin,
    TenantSwitch,
    WorkspaceCreate,
    WorkspacePatch,
)


def register_session_routes(app: FastAPI, rs: RouteState) -> None:
    ctx = rs.ctx
    require_runs = rs.require_runs
    _optional_console_user = rs.optional_console_user
    _resolve_tenant = rs.resolve_tenant
    _require_workspace_admin = rs.require_workspace_admin
    _require_library_write = rs.require_library_write
    _public_user = rs.public_user

    # ----- C3 auth -----
    @app.get("/v1/me")
    def me(request: Request) -> dict[str, Any]:
        user = _optional_console_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="console authentication required")
        return {
            "user_id": user.user_id,
            "display_name": user.display_name,
            "roles": sorted(user.roles),
            "tenants": list(user.tenants),
            "active_tenant": user.active_tenant,
            "auth_mode": auth_mode(),
        }

    @app.post("/v1/auth/dev-login")
    def dev_login(body: DevLogin, response: Response) -> dict[str, Any]:
        if not dev_login_enabled():
            raise HTTPException(status_code=404, detail="dev login disabled")
        requested = frozenset(body.roles) or frozenset({"operator"})
        if "admin" in requested and not dev_admin_enabled():
            raise HTTPException(
                status_code=403,
                detail="admin via dev-login requires RECERTIA_CONSOLE_DEV_ADMIN=1",
            )
        tenants = tuple(body.tenants) or ("default",)
        active = body.active_tenant or tenants[0]
        user = ConsoleUser(
            user_id=body.user_id,
            display_name=body.display_name,
            roles=requested,
            tenants=tenants,
            active_tenant=active if active in tenants else tenants[0],
        )
        token = ctx.sessions.issue(user)
        response.set_cookie("recertia_session", token, **session_cookie_kwargs())
        return {"ok": True, "user": _public_user(user)}

    @app.post("/v1/auth/logout")
    def logout(response: Response) -> dict[str, str]:
        response.delete_cookie("recertia_session", path="/")
        return {"status": "ok"}

    @app.post("/v1/auth/switch-tenant")
    def switch_tenant(body: TenantSwitch, request: Request, response: Response) -> dict[str, Any]:
        user = _optional_console_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="console authentication required")
        try:
            switched = ctx.sessions.switch_tenant(user, body.tenant_id)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        token = ctx.sessions.issue(switched)
        response.set_cookie("recertia_session", token, **session_cookie_kwargs())
        return {"active_tenant": switched.active_tenant, "tenants": list(switched.tenants)}

    # ----- Registered workspaces (RW0) -----
    @app.get("/v1/workspaces")
    def list_workspaces(
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        if ctx.workspace_registry is None:
            raise HTTPException(status_code=500, detail="workspace registry unavailable")
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        items = ctx.workspace_registry.list(tenant_id=tenant_id)
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
        ws = ctx.workspace_registry.get(workspace_id, tenant_id=tenant_id)
        if ws is None:
            raise HTTPException(status_code=404, detail="workspace not found")
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
        actor = _require_workspace_admin(request, principal)
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        from recertia.paths import HostRootError

        try:
            ws = ctx.workspace_registry.register(
                tenant_id=tenant_id,
                workspace_id=body.workspace_id,
                display_name=body.display_name,
                host_root=body.host_root,
                created_by=actor,
                notes=body.notes,
            )
        except LookupError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (HostRootError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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
        _require_workspace_admin(request, principal)
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        try:
            ws = ctx.workspace_registry.patch(
                workspace_id,
                tenant_id=tenant_id,
                display_name=body.display_name,
                notes=body.notes,
                enabled=body.enabled,
                clear_notes=body.clear_notes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if ws is None:
            raise HTTPException(status_code=404, detail="workspace not found")
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
        _require_workspace_admin(request, principal)
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        ws = ctx.workspace_registry.set_enabled(workspace_id, tenant_id=tenant_id, enabled=False)
        if ws is None:
            raise HTTPException(status_code=404, detail="workspace not found")
        return ws.model_dump(mode="json")

    @app.get("/v1/auth/oidc/login")
    def oidc_login(request: Request) -> dict[str, str]:
        if auth_mode() != "oidc" or not oidc_configured():
            raise HTTPException(status_code=404, detail="oidc not configured")
        redirect = str(request.url_for("oidc_callback"))
        state, verifier = ctx.sessions.begin_oidc(redirect_uri=redirect)
        return {
            "authorize_url": oidc_authorize_url(
                redirect_uri=redirect,
                state=state,
                code_challenge=pkce_challenge(verifier),
            ),
            "state": state,
        }

    @app.get("/v1/auth/oidc/callback", name="oidc_callback")
    def oidc_callback(
        request: Request, response: Response, code: str = "", state: str = ""
    ) -> dict[str, Any]:
        if auth_mode() != "oidc" or not oidc_configured():
            raise HTTPException(status_code=404, detail="oidc not configured")
        pending = ctx.sessions.take_oidc(state)
        if pending is None:
            raise HTTPException(status_code=400, detail="invalid or expired oidc state")
        user = oidc_exchange_code(
            code=code, redirect_uri=pending.redirect_uri, code_verifier=pending.verifier
        )
        token = ctx.sessions.issue(user)
        response.set_cookie("recertia_session", token, **session_cookie_kwargs())
        return {"ok": True, "user_id": user.user_id, "tenants": list(user.tenants)}

