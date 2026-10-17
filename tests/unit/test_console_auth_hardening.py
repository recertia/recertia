"""Console auth defaults, OIDC state/PKCE, fetch HTTPS-only."""

from __future__ import annotations

from pathlib import Path

import pytest

from recertia.api.console_auth import (
    SessionStore,
    _map_oidc_roles,
    oidc_authorize_url,
    pkce_challenge,
    resolve_session_secret,
)
from recertia.solver.registry import _host_allowed
from tests.support.http import error_text

pytest.importorskip("fastapi")


def test_oidc_requires_session_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECERTIA_CONSOLE_AUTH", "oidc")
    monkeypatch.delenv("RECERTIA_CONSOLE_SESSION_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        resolve_session_secret()


def test_oidc_state_must_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from recertia.api import create_app

    monkeypatch.setenv("RECERTIA_CONSOLE_AUTH", "oidc")
    monkeypatch.setenv("RECERTIA_CONSOLE_SESSION_SECRET", "s" * 32)
    monkeypatch.setenv("RECERTIA_OIDC_ISSUER", "https://idp.example")
    monkeypatch.setenv("RECERTIA_OIDC_CLIENT_ID", "cid")
    monkeypatch.setenv("RECERTIA_OIDC_CLIENT_SECRET", "csecret")
    app = create_app(root=tmp_path / "api-root")
    client = TestClient(app)
    start = client.get("/v1/auth/oidc/login")
    assert start.status_code == 200
    url = start.json()["authorize_url"]
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url
    assert "nonce=" in url
    bad = client.get("/v1/auth/oidc/callback", params={"code": "x", "state": "forged"})
    assert bad.status_code == 400
    assert "state" in error_text(bad)


def test_oidc_pkce_challenge_is_s256(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECERTIA_OIDC_ISSUER", "https://idp.example")
    monkeypatch.setenv("RECERTIA_OIDC_CLIENT_ID", "cid")
    verifier = "a" * 64
    challenge = pkce_challenge(verifier)
    url = oidc_authorize_url(
        redirect_uri="https://app.example/cb",
        state="st",
        code_challenge=challenge,
        nonce="n-1",
    )
    assert challenge in url
    assert "nonce=n-1" in url
    store = SessionStore(secret="k" * 32)
    state, ver, nonce = store.begin_oidc(redirect_uri="https://app.example/cb")
    taken = store.take_oidc(state)
    assert taken is not None
    assert taken.verifier == ver
    assert taken.nonce == nonce
    assert store.take_oidc(state) is None


def test_oidc_id_token_checks_nonce_and_jti() -> None:
    import base64
    import json

    from fastapi import HTTPException

    from recertia.api.console_auth import _check_id_token

    def token(payload: dict[str, str]) -> str:
        body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        return f"h.{body}.s"

    store = SessionStore(secret="k" * 32)
    nonce = "nonce-value"
    _check_id_token(token({"nonce": nonce, "jti": "j1"}), nonce=nonce, sessions=store)
    with pytest.raises(HTTPException) as duplicate:
        _check_id_token(token({"nonce": nonce, "jti": "j1"}), nonce=nonce, sessions=store)
    assert "duplicate" in str(duplicate.value.detail)
    with pytest.raises(HTTPException) as mismatched:
        _check_id_token(
            token({"nonce": "other-nonce", "jti": "j2"}), nonce=nonce, sessions=store
        )
    assert "nonce" in str(mismatched.value.detail)
    with pytest.raises(HTTPException) as missing:
        _check_id_token(token({"nonce": nonce}), nonce=nonce, sessions=store)
    assert "jti" in str(missing.value.detail)


def test_oidc_roles_never_default_to_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECERTIA_OIDC_ROLE_ALLOWLIST", raising=False)
    monkeypatch.setenv("RECERTIA_OIDC_DEFAULT_ROLES", "admin")
    roles = _map_oidc_roles({})
    assert "admin" not in roles
    monkeypatch.setenv("RECERTIA_OIDC_ROLE_ALLOWLIST", "operator,reviewer")
    roles = _map_oidc_roles({"roles": ["admin"]})
    assert "admin" not in roles


def test_dev_login_admin_requires_second_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from recertia.api import create_app

    monkeypatch.setenv("RECERTIA_CONSOLE_AUTH", "dev")
    monkeypatch.setenv("RECERTIA_CONSOLE_DEV_LOGIN", "1")
    monkeypatch.setenv("RECERTIA_CONSOLE_SESSION_SECRET", "t" * 32)
    monkeypatch.setenv("RECERTIA_CONSOLE_COOKIE_SECURE", "0")
    monkeypatch.delenv("RECERTIA_CONSOLE_DEV_ADMIN", raising=False)
    app = create_app(root=tmp_path / "api-root")
    client = TestClient(app)
    denied = client.post(
        "/v1/auth/dev-login",
        json={"user_id": "x", "roles": ["admin"], "tenants": ["t1"]},
    )
    assert denied.status_code == 403
    ok = client.post(
        "/v1/auth/dev-login",
        json={"user_id": "x", "roles": ["operator"], "tenants": ["t1"]},
    )
    assert ok.status_code == 200
    assert "session" not in ok.json()
    assert client.cookies.get("recertia_session")


def test_fetch_rejects_http_and_suffix_allowlist(tmp_path: Path) -> None:
    from recertia.governance.sandbox import ApprovalGate
    from recertia.solver.registry import default_registry
    from recertia.solver.runtime import ToolRuntime

    gate = ApprovalGate()
    registry = default_registry()
    for name in registry.names():
        gate.approve(name, actor="t", reason="t")
    runtime = ToolRuntime(registry, approval_gate=gate)
    http = runtime.invoke(
        "fetch", {"url": "http://pypi.org/pypi/demo/json"}, workdir=tmp_path, step_id="h"
    )
    assert not http.ok
    assert "unsupported" in http.stderr
    assert not _host_allowed("evil.com", ("com",))
    assert _host_allowed("pypi.org", ("pypi.org",))
    assert not _host_allowed("notpypi.org", ("pypi.org",))
    assert not _host_allowed("evil.pypi.org", ("pypi.org",))
