"""Per-request helpers. ``get_state`` is the FastAPI dependency for ``AppState``."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from recertia.api.console_auth import ConsoleUser, console_session
from recertia.app.state import AppState


def get_state(request: Request) -> AppState:
    state = getattr(request.app.state, "recertia", None)
    if state is None:
        raise RuntimeError("AppState missing on app.state.recertia")
    return state


class RouteState:
    """Shared closures for split route modules."""

    def __init__(self, ctx: AppState) -> None:
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
