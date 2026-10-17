"""Resolve one principal from a request. API key XOR session; the two are not merged."""

from __future__ import annotations

from typing import Callable

from fastapi import Header, HTTPException, Request

from recertia.api.auth import ApiKeyStore
from recertia.errors import Forbidden, Unauthorized
from recertia.ports.identity import Principal, TenantId, tenant_id

_ROLE_SCOPES = {
    "operator": frozenset({"runs", "blobs", "metrics"}),
    "reviewer": frozenset({"runs", "blobs", "metrics", "promote", "jobs", "evals"}),
    "admin": frozenset({"runs", "blobs", "metrics", "promote", "jobs", "evals", "admin", "exec"}),
    "platform_admin": frozenset(
        {"runs", "blobs", "metrics", "promote", "jobs", "evals", "admin", "exec", "platform_admin"}
    ),
}


def _scopes_for_roles(roles: frozenset[str]) -> frozenset[str]:
    scopes: set[str] = set()
    for role in roles:
        scopes |= _ROLE_SCOPES.get(role, frozenset())
    return frozenset(scopes)


def resolve_principal(
    request: Request,
    x_api_key: str | None,
    *,
    keys: ApiKeyStore,
    session_user: object | None = None,
) -> Principal:
    """API key or console session. Both presented is ``Forbidden`` (no merge)."""

    from recertia.api.console_auth import auth_mode

    sessions_on = auth_mode() != "off"
    cookie = request.cookies.get("recertia_session") if request is not None and sessions_on else None
    header_session = (
        request.headers.get("x-recertia-session") if request is not None and sessions_on else None
    )
    if not sessions_on:
        session_user = None
    has_session = bool(cookie or header_session or session_user is not None)
    has_key = bool(x_api_key)
    if has_key and has_session:
        raise Forbidden("present an API key or a session, not both")
    if has_key:
        principal = keys.authenticate(x_api_key)
        if principal is None:
            raise Unauthorized("invalid API key")
        return principal
    if session_user is not None:
        return principal_from_session_user(session_user)
    raise Unauthorized("authentication required")


def principal_from_session_user(user: object) -> Principal:
    subject = str(getattr(user, "user_id"))
    roles = frozenset(getattr(user, "roles"))
    raw_tenants = tuple(getattr(user, "tenants"))
    tenants = tuple(tenant_id(item) for item in raw_tenants)
    active_raw = str(getattr(user, "active_tenant"))
    active = tenant_id(active_raw) if active_raw in {str(item) for item in tenants} else tenants[0]
    return Principal(
        subject=subject,
        kind="console_session",
        tenant=active,
        tenants=tenants,
        scopes=_scopes_for_roles(roles),
        roles=roles,
    )


def resolve_tenant(principal: Principal, requested: str | None) -> TenantId:
    """The requested tenant must already be on the principal. No header invents one."""

    if requested is None or requested == "":
        return principal.tenant
    wanted = tenant_id(requested)
    if wanted not in principal.tenants and wanted != principal.tenant:
        raise Forbidden("tenant is not in membership")
    return wanted


def require(scope: str, store: ApiKeyStore) -> Callable[..., Principal]:
    """FastAPI dependency. 401 when unresolved, 403 when the scope is missing."""

    def dependency(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> Principal:
        try:
            principal = resolve_principal(request, x_api_key, keys=store)
        except Unauthorized as exc:
            raise HTTPException(status_code=401, detail=exc.message) from exc
        except Forbidden as exc:
            raise HTTPException(status_code=403, detail=exc.message) from exc
        if not principal.has_scope(scope):
            raise HTTPException(status_code=403, detail=f"missing scope: {scope}")
        return principal

    return dependency
