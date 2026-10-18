"""Registered host workspaces. CLI and HTTP share this service."""

from __future__ import annotations

from typing import Any

from recertia.app.authz import Resource
from recertia.errors import ConfigError, Conflict, NotFound
from recertia.paths import HostRootError
from recertia.ports.identity import Principal


class WorkspaceService:
    """``pdp.require`` then ``WorkspaceRegistry``. Routes do not call register/patch."""

    def __init__(self, *, pdp: Any | None = None) -> None:
        self._pdp = pdp

    def list(self, principal: Principal, *, registry: Any, tenant_id: str) -> list[Any]:
        self._require(principal, "workspaces.read")
        return registry.list(tenant_id=tenant_id)

    def get(
        self, principal: Principal, *, registry: Any, tenant_id: str, workspace_id: str
    ) -> Any:
        self._require(principal, "workspaces.read")
        ws = registry.get(workspace_id, tenant_id=tenant_id)
        if ws is None:
            raise NotFound("workspace not found")
        return ws

    def register(
        self,
        principal: Principal,
        *,
        registry: Any,
        tenant_id: str,
        workspace_id: str,
        display_name: str,
        host_root: str,
        created_by: str,
        notes: str | None = None,
        console_roles: frozenset[str] | None = None,
    ) -> Any:
        self._require_register(principal, console_roles=console_roles)
        try:
            return registry.register(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                display_name=display_name,
                host_root=host_root,
                created_by=created_by,
                notes=notes,
            )
        except LookupError as exc:
            raise Conflict(str(exc)) from exc
        except (HostRootError, ValueError) as exc:
            raise ConfigError(str(exc)) from exc

    def update(
        self,
        principal: Principal,
        *,
        registry: Any,
        tenant_id: str,
        workspace_id: str,
        display_name: str | None = None,
        notes: str | None = None,
        enabled: bool | None = None,
        clear_notes: bool = False,
        console_is_admin: bool | None = None,
    ) -> Any:
        self._require_write(principal, console_is_admin=console_is_admin)
        try:
            ws = registry.patch(
                workspace_id,
                tenant_id=tenant_id,
                display_name=display_name,
                notes=notes,
                enabled=enabled,
                clear_notes=clear_notes,
            )
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        if ws is None:
            raise NotFound("workspace not found")
        return ws

    def disable(
        self,
        principal: Principal,
        *,
        registry: Any,
        tenant_id: str,
        workspace_id: str,
        console_is_admin: bool | None = None,
    ) -> Any:
        self._require_write(principal, console_is_admin=console_is_admin)
        ws = registry.set_enabled(workspace_id, tenant_id=tenant_id, enabled=False)
        if ws is None:
            raise NotFound("workspace not found")
        return ws

    def delete(
        self,
        principal: Principal,
        *,
        registry: Any,
        tenant_id: str,
        workspace_id: str,
        console_is_admin: bool | None = None,
    ) -> Any:
        return self.disable(
            principal,
            registry=registry,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            console_is_admin=console_is_admin,
        )

    def _require(self, principal: Principal, action: str) -> None:
        pdp = self._pdp
        if pdp is None:
            return
        pdp.require(principal, action, Resource(tenant_id=principal.tenant_id))

    def _require_register(
        self, principal: Principal, *, console_roles: frozenset[str] | None
    ) -> None:
        pdp = self._pdp
        if pdp is None:
            return
        if console_roles is not None:
            if pdp.may_admin_host(principal, console_roles=console_roles):
                return
            from recertia.errors import Forbidden

            raise Forbidden("platform_admin required to register a host root")
        pdp.require(principal, "workspace.register", Resource(tenant_id=principal.tenant_id))

    def _require_write(
        self, principal: Principal, *, console_is_admin: bool | None
    ) -> None:
        pdp = self._pdp
        if pdp is None:
            return
        if console_is_admin is not None:
            if pdp.may_admin_registry(principal, console_is_admin=console_is_admin):
                return
            from recertia.errors import Forbidden

            raise Forbidden("admin required to update workspace")
        pdp.require(principal, "workspaces.write", Resource(tenant_id=principal.tenant_id))
