"""Durable, scoped API-key authentication."""

from __future__ import annotations

from typing import Any, Callable

from recertia.persistence.sqlite.api_keys import (
    ApiKeyStore,
    IssuedApiKey,
    validate_scopes,
    validate_tenant_id,
)
from recertia.ports.identity import Principal

__all__ = [
    "ApiKeyStore",
    "IssuedApiKey",
    "Principal",
    "require_key",
    "require_scope",
    "validate_scopes",
    "validate_tenant_id",
]


def _optional_api_key_header() -> Any:
    """Header extractor when FastAPI is installed; a plain default otherwise."""

    try:
        from fastapi import Header
    except ImportError:
        return None
    return Header(default=None, alias="X-API-Key")


def require_key(
    required_scope: str,
    store: ApiKeyStore,
    x_api_key: str | None = _optional_api_key_header(),
    *,
    client_ip: str = "unknown",
) -> Principal:
    from fastapi import HTTPException

    principal = store.authenticate(x_api_key, client_ip=client_ip)
    if principal is None:
        raise HTTPException(status_code=401, detail="invalid API key")
    if required_scope not in principal.scopes and "admin" not in principal.scopes:
        raise HTTPException(status_code=403, detail=f"missing scope: {required_scope}")
    return principal


def require_scope(scope: str, store: ApiKeyStore) -> Callable[..., Principal]:
    """FastAPI dependency factory preserving the header parameter signature."""

    from fastapi import Header, Request

    # Nested annotations resolve against this module, not the enclosing function.
    globals()["Request"] = Request

    def dependency(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> Principal:
        host = request.client.host if request.client is not None else "unknown"
        return require_key(scope, store, x_api_key, client_ip=host)

    return dependency
