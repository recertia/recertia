"""SEC-8: platform_admin host registration, workspace roots, tenant skills."""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.policy import Policy
from recertia.app.authz import PolicyDecisionPoint
from recertia.errors import ConfigError
from recertia.paths import HostRootError, assert_host_root_allowed, tenant_skills_root
from recertia.ports.identity import Principal
from recertia.settings import load_settings
from recertia.workspaces.registry import WorkspaceRegistry


def test_tenant_admin_is_not_platform_admin() -> None:
    tenant_admin = Principal.from_api_key(
        key_id="admin", tenant_id="t1", scopes=frozenset({"runs", "admin"})
    )
    host = Principal.from_api_key(
        key_id="host", tenant_id="t1", scopes=frozenset({"runs", "platform_admin"})
    )
    pdp = PolicyDecisionPoint(Policy(version="s", authoring_prior_version="s"), "local")
    assert tenant_admin.has_scope("runs")
    assert not tenant_admin.has_scope("platform_admin")
    assert pdp.may_admin_registry(tenant_admin)
    assert not pdp.may_admin_host(tenant_admin)
    assert pdp.may_admin_host(host)
    assert pdp.may_admin_host(Principal.local_operator())


def test_require_key_tenant_admin_is_not_platform_admin(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    from recertia.api.auth import ApiKeyStore, require_key

    store = ApiKeyStore(tmp_path / "keys.sqlite")
    tenant_admin = store.issue(tenant_id="t1", scopes={"runs", "admin"}, actor="test")
    platform = store.issue(
        tenant_id="t1", scopes={"runs", "platform_admin"}, actor="test"
    )
    principal = store.authenticate(tenant_admin.secret)
    assert principal is not None
    assert principal.has_scope("runs")
    assert not principal.has_scope("platform_admin")
    with pytest.raises(HTTPException) as denied:
        require_key("platform_admin", store, tenant_admin.secret)
    assert denied.value.status_code == 403
    assert require_key("runs", store, tenant_admin.secret).has_scope("runs")
    got = require_key("platform_admin", store, platform.secret)
    assert got.has_scope("platform_admin")


def test_workspace_host_root_denies_recertia_and_honors_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    denied = tmp_path / ".recertia"
    denied.mkdir()
    with pytest.raises(HostRootError, match=".recertia"):
        assert_host_root_allowed(str(denied))

    allowed = tmp_path / "repos"
    other = tmp_path / "other"
    allowed.mkdir()
    other.mkdir()
    (allowed / "proj").mkdir()
    monkeypatch.setenv("RECERTIA_WORKSPACE_ROOTS", str(allowed))
    assert Path(assert_host_root_allowed(str(allowed / "proj"))) == (allowed / "proj").resolve()
    with pytest.raises(HostRootError, match="RECERTIA_WORKSPACE_ROOTS"):
        assert_host_root_allowed(str(other))

    registry = WorkspaceRegistry(tmp_path / "reg.sqlite")
    try:
        with pytest.raises(HostRootError, match=".recertia"):
            registry.register(
                tenant_id="t1",
                workspace_id="secret",
                display_name="nope",
                host_root=str(denied),
                created_by="test",
            )
    finally:
        registry.close()


def test_multi_node_requires_tenant_skills_and_workspace_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RECERTIA_DEPLOYMENT_MODE", "multi_node")
    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "container")
    monkeypatch.setenv("RECERTIA_DB_BACKEND", "postgres")
    monkeypatch.setenv("RECERTIA_DATABASE_URL", "postgres://db")
    monkeypatch.setenv("RECERTIA_BLOB_STORE", "s3")
    monkeypatch.setenv("RECERTIA_CONSOLE_SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("RECERTIA_CONSOLE_AUTH", "oidc")
    monkeypatch.setenv("RECERTIA_API_KEY_PEPPER", "pepper")
    with pytest.raises(ConfigError, match="RECERTIA_TENANT_SKILLS"):
        load_settings().validate_deployment(role="api")
    monkeypatch.setenv("RECERTIA_TENANT_SKILLS", "1")
    with pytest.raises(ConfigError, match="RECERTIA_WORKSPACE_ROOTS"):
        load_settings().validate_deployment(role="api")
    monkeypatch.setenv("RECERTIA_WORKSPACE_ROOTS", str(tmp_path))
    load_settings().validate_deployment(role="api")


def test_tenant_skills_root_stays_shared_until_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "skills"
    shared.mkdir()
    monkeypatch.delenv("RECERTIA_TENANT_SKILLS", raising=False)
    assert (
        tenant_skills_root(control_root=tmp_path, shared=shared, tenant_id="t1") == shared
    )
    monkeypatch.setenv("RECERTIA_TENANT_SKILLS", "1")
    got = tenant_skills_root(control_root=tmp_path, shared=shared, tenant_id="t1")
    assert got == tmp_path / "tenants" / "t1" / "skills"
    assert got.is_dir()


def test_create_run_and_worker_use_tenant_skills_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from recertia.api import create_app

    monkeypatch.setenv("RECERTIA_TENANT_SKILLS", "1")
    seen: list[Path] = []

    import recertia.api as api_mod
    import recertia.workers.run_worker as worker_mod

    real_build = api_mod.build_default_orchestrator

    def _record(*args, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(Path(kwargs["skills_root"]))
        return real_build(*args, **kwargs)

    monkeypatch.setattr(api_mod, "build_default_orchestrator", _record)
    monkeypatch.setattr(worker_mod, "build_default_orchestrator", _record)
    app = create_app(root=tmp_path / "api-root", skills_root=tmp_path / "skills")
    issued = app.state.api_keys.issue(tenant_id="t1", scopes={"runs", "exec"}, actor="test")
    client = TestClient(app)
    created = client.post(
        "/v1/runs",
        json={
            "request": "write output.txt",
            "run_id": "skills-1",
            "script": ["true"],
            "budget": {"max_attempts": 1},
        },
        headers={"X-API-Key": issued.secret},
    )
    assert created.status_code == 200, created.text
    expected = tmp_path / "api-root" / "tenants" / "t1" / "skills"
    assert seen and seen[0] == expected

    async_created = client.post(
        "/v1/runs",
        json={
            "request": "queued",
            "run_id": "skills-async",
            "mode": "async",
            "script": ["true"],
            "budget": {"max_attempts": 1},
        },
        headers={"X-API-Key": issued.secret},
    )
    assert async_created.status_code == 202, async_created.text
    app.state.console_ctx.embedded.flush(timeout=30)
    assert expected in seen


def test_http_register_host_requires_platform_admin(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from recertia.api import create_app

    app = create_app(root=tmp_path / "api-root")
    tenant_admin = app.state.api_keys.issue(
        tenant_id="t1", scopes={"runs", "admin"}, actor="test"
    )
    host = app.state.api_keys.issue(
        tenant_id="t1", scopes={"runs", "admin", "platform_admin"}, actor="test"
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    client = TestClient(app)
    body = {"workspace_id": "w1", "display_name": "r", "host_root": str(repo)}
    denied = client.post(
        "/v1/workspaces", json=body, headers={"X-API-Key": tenant_admin.secret}
    )
    assert denied.status_code == 403
    allowed = client.post("/v1/workspaces", json=body, headers={"X-API-Key": host.secret})
    assert allowed.status_code == 201, allowed.text
