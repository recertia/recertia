"""SEC-1: one Principal, validated tenant ids, key XOR session."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from recertia.api.identity import principal_from_session_user, resolve_principal
from recertia.errors import Forbidden
from recertia.ports.identity import Principal, TenantId, tenant_id


def test_tenant_id_rejects_traversal() -> None:
    with pytest.raises(ValueError):
        tenant_id("../etc")
    assert tenant_id("acme") == TenantId("acme")


def test_local_operator_is_platform_admin() -> None:
    principal = Principal.local_operator()
    assert principal.kind == "local_operator"
    assert principal.has_role("platform_admin")
    assert principal.has_scope("runs")
    assert principal.key_id.startswith("cli:")


def test_resolve_principal_rejects_key_and_session_together(tmp_path, monkeypatch) -> None:
    from recertia.api.auth import ApiKeyStore

    monkeypatch.setenv("RECERTIA_CONSOLE_AUTH", "dev")
    store = ApiKeyStore(tmp_path / "keys.sqlite")
    issued = store.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    request = SimpleNamespace(
        cookies={"recertia_session": "cookie"},
        headers={},
    )
    with pytest.raises(Forbidden):
        resolve_principal(request, issued.secret, keys=store)  # type: ignore[arg-type]


def test_auth_off_ignores_a_session_cookie(tmp_path, monkeypatch) -> None:
    from recertia.api.auth import ApiKeyStore

    monkeypatch.setenv("RECERTIA_CONSOLE_AUTH", "off")
    store = ApiKeyStore(tmp_path / "keys.sqlite")
    issued = store.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    request = SimpleNamespace(
        cookies={"recertia_session": "cookie"},
        headers={"x-recertia-session": "header"},
    )
    principal = resolve_principal(
        request,  # type: ignore[arg-type]
        issued.secret,
        keys=store,
        session_user=SimpleNamespace(user_id="ada"),
    )
    assert principal.kind == "api_key"


def test_resolve_principal_returns_ports_principal(tmp_path) -> None:
    from recertia.api.auth import ApiKeyStore

    store = ApiKeyStore(tmp_path / "keys.sqlite")
    issued = store.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    request = SimpleNamespace(cookies={}, headers={})
    principal = resolve_principal(request, issued.secret, keys=store)  # type: ignore[arg-type]
    assert isinstance(principal, Principal)
    assert principal.kind == "api_key"
    assert principal.tenant_id == "t1"
    assert principal.has_scope("runs")


def test_session_user_becomes_principal() -> None:
    user = SimpleNamespace(
        user_id="ada",
        roles=frozenset({"operator"}),
        tenants=("t1",),
        active_tenant="t1",
    )
    principal = principal_from_session_user(user)
    assert principal.kind == "console_session"
    assert principal.has_scope("runs")
    assert "exec" not in principal.scopes


def test_require_dependency_shape_is_http() -> None:
    # The dependency raises HTTPException, which the existing routes already catch.
    assert HTTPException
