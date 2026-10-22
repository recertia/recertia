"""HTTP adapters. Handlers map RecertiaError to HTTP; services own policy."""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response

from recertia.api.console_auth import (
    ConsoleUser,
    auth_mode,
    csrf_cookie_kwargs,
    dev_admin_enabled,
    dev_login_enabled,
    oidc_authorize_url,
    oidc_client_key,
    oidc_configured,
    oidc_exchange_code,
    parse_trusted_proxies,
    pkce_challenge,
    session_cookie_kwargs,
)
from recertia.api.deps import RouteState
from recertia.api.schemas import DevLogin, TenantSwitch
from recertia.errors import Forbidden


def _set_session_cookies(response: Response, sessions: Any, token: str) -> None:
    response.set_cookie("recertia_session", token, **session_cookie_kwargs())
    csrf = sessions.csrf_for(token)
    if csrf:
        response.set_cookie("recertia_csrf", csrf, **csrf_cookie_kwargs())


def _require_cookie_csrf(request: Request, sessions: Any) -> None:
    session_cookie = request.cookies.get("recertia_session")
    if not session_cookie:
        return
    header = request.headers.get("x-recertia-csrf") or ""
    cookie = request.cookies.get("recertia_csrf") or ""
    if not header or len(header) != len(cookie) or not hmac.compare_digest(header, cookie):
        raise HTTPException(status_code=403, detail="csrf rejected")
    if not sessions.csrf_matches(session_cookie, header):
        raise HTTPException(status_code=403, detail="csrf rejected")


def _reject_foreign_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    host = request.headers.get("host") or ""
    allowed = {f"http://{host}", f"https://{host}"}
    if origin.rstrip("/") not in allowed:
        raise HTTPException(status_code=403, detail="origin rejected")


def register_auth_routes(app: FastAPI, rs: RouteState) -> None:
    ctx = rs.ctx
    _optional_console_user = rs.optional_console_user
    _resolve_tenant = rs.resolve_tenant
    _require_library_write = rs.require_library_write
    _public_user = rs.public_user
    trusted_proxies = parse_trusted_proxies(ctx.settings.trusted_proxies)

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
    def dev_login(body: DevLogin, request: Request, response: Response) -> dict[str, Any]:
        _reject_foreign_origin(request)
        _require_cookie_csrf(request, ctx.sessions)
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
            active_tenant=active,
        )
        try:
            token = ctx.sessions.issue(user)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _set_session_cookies(response, ctx.sessions, token)
        return {"ok": True, "user": _public_user(user)}
    @app.post("/v1/auth/logout")
    def logout(request: Request, response: Response) -> dict[str, str]:
        _reject_foreign_origin(request)
        _require_cookie_csrf(request, ctx.sessions)
        response.delete_cookie("recertia_session", path="/")
        response.delete_cookie("recertia_csrf", path="/")
        return {"status": "ok"}
    @app.post("/v1/auth/switch-tenant")
    def switch_tenant(body: TenantSwitch, request: Request, response: Response) -> dict[str, Any]:
        _reject_foreign_origin(request)
        _require_cookie_csrf(request, ctx.sessions)
        user = _optional_console_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="console authentication required")
        from recertia.api.identity import principal_from_session_user

        try:
            ctx.pdp.require(principal_from_session_user(user), "tenants.switch")
        except Forbidden as exc:
            raise HTTPException(status_code=403, detail=exc.message) from exc
        try:
            switched = ctx.sessions.switch_tenant(user, body.tenant_id)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        token = ctx.sessions.issue(switched)
        _set_session_cookies(response, ctx.sessions, token)
        return {"active_tenant": switched.active_tenant, "tenants": list(switched.tenants)}

    # ----- Registered workspaces (RW0) -----
    @app.get("/v1/auth/oidc/login")
    def oidc_login(request: Request) -> dict[str, str]:
        if auth_mode() != "oidc" or not oidc_configured():
            raise HTTPException(status_code=404, detail="oidc not configured")
        redirect = str(request.url_for("oidc_callback"))
        # Client key for the per-client pending cap (REC-056a): see oidc_client_key. Every
        # X-Forwarded-For line is passed, in order; Forwarded is never read.
        client_key = oidc_client_key(
            request.client.host if request.client else None,
            request.headers.getlist("x-forwarded-for"),
            trusted_proxies,
            request.client.port if request.client else None,
        )
        state, verifier, nonce = ctx.sessions.begin_oidc(redirect_uri=redirect, client_key=client_key)
        return {
            "authorize_url": oidc_authorize_url(
                redirect_uri=redirect,
                state=state,
                code_challenge=pkce_challenge(verifier),
                nonce=nonce,
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
            code=code,
            redirect_uri=pending.redirect_uri,
            code_verifier=pending.verifier,
            nonce=pending.nonce,
            sessions=ctx.sessions,
        )
        token = ctx.sessions.issue(user)
        _set_session_cookies(response, ctx.sessions, token)
        return {"ok": True, "user_id": user.user_id, "tenants": list(user.tenants)}

