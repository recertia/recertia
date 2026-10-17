"""SEC-10: sessions off by default, SameSite=Strict, Origin on cookie POSTs."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recertia.api import create_app
from recertia.api.console_auth import session_cookie_kwargs
from recertia.errors import ConfigError
from recertia.settings import load_settings


def test_cookie_v2_keeps_roles_off_the_wire() -> None:
    from recertia.api.console_auth import ConsoleUser, SessionStore

    store = SessionStore(secret="k" * 32)
    token = store.issue(
        ConsoleUser(
            user_id="ada",
            display_name="Ada",
            roles=frozenset({"operator"}),
            tenants=("t1",),
            active_tenant="t1",
        )
    )
    assert token.startswith("v2.")
    assert "operator" not in token
    parsed = store.parse(token)
    assert parsed is not None
    assert "operator" in parsed.roles
    assert store.parse('{"roles":["admin"]}.deadbeef') is None


def test_cookie_post_requires_double_submit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_CONSOLE_AUTH", "dev")
    monkeypatch.setenv("RECERTIA_CONSOLE_DEV_LOGIN", "1")
    monkeypatch.setenv("RECERTIA_CONSOLE_SESSION_SECRET", "t" * 32)
    monkeypatch.setenv("RECERTIA_CONSOLE_COOKIE_SECURE", "0")
    app = create_app(root=tmp_path / "api-root")
    client = TestClient(app)
    body = {"user_id": "ada", "roles": ["operator"], "tenants": ["t1", "t2"]}
    login = client.post("/v1/auth/dev-login", json=body)
    assert login.status_code == 200
    csrf = client.cookies.get("recertia_csrf")
    assert csrf
    assert "samesite=strict" in login.headers["set-cookie"].lower()
    missing = client.post("/v1/auth/switch-tenant", json={"tenant_id": "t2"})
    assert missing.status_code == 403
    switched = client.post(
        "/v1/auth/switch-tenant",
        json={"tenant_id": "t2"},
        headers={"X-Recertia-CSRF": csrf},
    )
    assert switched.status_code == 200
    logged_out = client.post(
        "/v1/auth/logout",
        headers={"X-Recertia-CSRF": client.cookies.get("recertia_csrf") or ""},
    )
    assert logged_out.status_code == 200


def test_session_cookie_is_samesite_strict() -> None:
    assert session_cookie_kwargs()["samesite"] == "strict"


def test_dev_login_rejects_foreign_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECERTIA_CONSOLE_AUTH", "dev")
    monkeypatch.setenv("RECERTIA_CONSOLE_DEV_LOGIN", "1")
    monkeypatch.setenv("RECERTIA_CONSOLE_SESSION_SECRET", "t" * 32)
    monkeypatch.setenv("RECERTIA_CONSOLE_COOKIE_SECURE", "0")
    app = create_app(root=tmp_path / "api-root")
    client = TestClient(app)
    body = {"user_id": "ada", "roles": ["operator"], "tenants": ["t1"]}
    denied = client.post("/v1/auth/dev-login", json=body, headers={"Origin": "https://evil.example"})
    assert denied.status_code == 403
    ok = client.post("/v1/auth/dev-login", json=body)
    assert ok.status_code == 200
    assert "samesite=strict" in ok.headers["set-cookie"].lower()
    logout = client.post("/v1/auth/logout", headers={"Origin": "https://evil.example"})
    assert logout.status_code == 403


def test_auth_off_does_not_honor_a_session_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_CONSOLE_AUTH", "off")
    app = create_app(root=tmp_path / "api-root")
    issued = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    client = TestClient(app)
    resp = client.get(
        "/v1/runs",
        headers={"X-API-Key": issued.secret, "X-Recertia-Session": "forged"},
    )
    assert resp.status_code == 200


def _multi_node_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Mirror test_sec8_privilege multi_node startup prerequisites."""

    monkeypatch.setenv("RECERTIA_DEPLOYMENT_MODE", "multi_node")
    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "container")
    monkeypatch.setenv("RECERTIA_DB_BACKEND", "postgres")
    monkeypatch.setenv("RECERTIA_DATABASE_URL", "postgres://db")
    monkeypatch.setenv("RECERTIA_BLOB_STORE", "s3")
    monkeypatch.setenv("RECERTIA_CONSOLE_SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("RECERTIA_API_KEY_PEPPER", "pepper")
    monkeypatch.setenv("RECERTIA_TENANT_SKILLS", "1")
    monkeypatch.setenv("RECERTIA_WORKSPACE_ROOTS", str(tmp_path))


def test_multi_node_refuses_console_auth_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _multi_node_env(monkeypatch, tmp_path)
    monkeypatch.setenv("RECERTIA_CONSOLE_AUTH", "off")
    with pytest.raises(ConfigError, match="RECERTIA_CONSOLE_AUTH=off"):
        load_settings().validate_deployment(role="api")


def test_multi_node_refuses_dev_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _multi_node_env(monkeypatch, tmp_path)
    monkeypatch.setenv("RECERTIA_CONSOLE_AUTH", "oidc")
    monkeypatch.setenv("RECERTIA_CONSOLE_DEV_LOGIN", "1")
    with pytest.raises(ConfigError, match="dev-login"):
        load_settings().validate_deployment(role="api")


def test_multi_node_accepts_oidc_with_session_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _multi_node_env(monkeypatch, tmp_path)
    monkeypatch.setenv("RECERTIA_CONSOLE_AUTH", "oidc")
    monkeypatch.delenv("RECERTIA_CONSOLE_DEV_LOGIN", raising=False)
    load_settings().validate_deployment(role="api")


def test_multi_node_refuses_dev_login_and_auth_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SEC-10c F22: combined refusal named in sec-specs.md."""

    _multi_node_env(monkeypatch, tmp_path)
    monkeypatch.delenv("RECERTIA_CONSOLE_AUTH", raising=False)  # defaults to off
    with pytest.raises(ConfigError, match="RECERTIA_CONSOLE_AUTH=off"):
        load_settings().validate_deployment(role="api")
    monkeypatch.setenv("RECERTIA_CONSOLE_AUTH", "oidc")
    monkeypatch.setenv("RECERTIA_CONSOLE_DEV_LOGIN", "1")
    with pytest.raises(ConfigError, match="dev-login"):
        load_settings().validate_deployment(role="api")
