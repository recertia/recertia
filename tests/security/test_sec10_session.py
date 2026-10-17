"""SEC-10: sessions off by default, SameSite=Strict, Origin on cookie POSTs."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recertia.api import create_app
from recertia.api.console_auth import session_cookie_kwargs


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
