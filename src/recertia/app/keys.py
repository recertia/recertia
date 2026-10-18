"""API key issue, revoke, and list. The store is injected; app does not import api."""

from __future__ import annotations

from typing import Any

from recertia.app.authz import Resource
from recertia.errors import NotFound
from recertia.ports.identity import Principal


class KeyService:
    """``pdp.require`` then the injected key store."""

    def __init__(self, *, pdp: Any | None = None) -> None:
        self._pdp = pdp

    def issue(
        self,
        principal: Principal,
        *,
        store: Any,
        tenant_id: str,
        scopes: set[str] | frozenset[str],
        actor: str,
    ) -> Any:
        self._require(principal, "keys.issue")
        return store.issue(tenant_id=tenant_id, scopes=scopes, actor=actor)

    def revoke(self, principal: Principal, *, store: Any, key_id: str, actor: str) -> None:
        self._require(principal, "keys.revoke")
        if not store.revoke(key_id, actor=actor):
            raise NotFound(f"key not found: {key_id}")

    def list(self, principal: Principal, *, store: Any) -> list[dict[str, Any]]:
        self._require(principal, "keys.read")
        return store.list_keys()

    def _require(self, principal: Principal, action: str) -> None:
        pdp = self._pdp
        if pdp is None:
            return
        pdp.require(principal, action, Resource(tenant_id=principal.tenant_id))
