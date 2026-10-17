"""API security: local fail-closed, exec scope for scripts, blob caps, key format."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from recertia.api import create_app
from recertia.api.auth import ApiKeyStore
from recertia.solver.container import ensure_api_execution_ready
from recertia.solver.sandbox import SandboxError
from tests.support.http import error_text
from tests.support.platform import skip_posix_mode_bits


def test_api_refuses_local_backend_without_break_glass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "local")
    monkeypatch.delenv("RECERTIA_API_ALLOW_LOCAL_EXEC", raising=False)
    with pytest.raises(SandboxError, match="not allowed for the HTTP API"):
        ensure_api_execution_ready()


def test_unstructured_api_key_is_rejected(tmp_path: Path) -> None:
    keys = ApiKeyStore(tmp_path / "keys.sqlite")
    keys.issue(tenant_id="t1", scopes={"runs"}, actor="op")
    assert keys.authenticate("fnd_legacy_not_structured") is None


def test_runs_key_cannot_promote_or_trigger_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "local")
    monkeypatch.setenv("RECERTIA_API_ALLOW_LOCAL_EXEC", "1")
    app = create_app(root=tmp_path / "api-root", skills_root=tmp_path / "skills")
    runs_only = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    client = TestClient(app)
    denied_promote = client.post(
        "/v1/skills/demo/versions/1/promote",
        headers={"X-API-Key": runs_only.secret},
    )
    assert denied_promote.status_code == 403
    assert "promote" in error_text(denied_promote)
    denied_job = client.post(
        "/v1/jobs/practice/run",
        headers={"X-API-Key": runs_only.secret},
        json={"dry_run": True},
    )
    assert denied_job.status_code == 403
    assert "jobs" in error_text(denied_job)


def test_dev_login_disabled_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECERTIA_CONSOLE_AUTH", raising=False)
    monkeypatch.delenv("RECERTIA_CONSOLE_DEV_LOGIN", raising=False)
    app = create_app(root=tmp_path / "api-root")
    client = TestClient(app)
    resp = client.post("/v1/auth/dev-login", json={"user_id": "x", "roles": ["admin"]})
    assert resp.status_code == 404


def test_script_requires_exec_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "local")
    monkeypatch.setenv("RECERTIA_API_ALLOW_LOCAL_EXEC", "1")
    app = create_app(root=tmp_path / "api-root")
    runs_only = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    client = TestClient(app)
    denied = client.post(
        "/v1/runs",
        json={
            "request": "x",
            "run_id": "needs-exec",
            "script": ["true"],
            "budget": {"max_attempts": 1},
        },
        headers={"X-API-Key": runs_only.secret},
    )
    assert denied.status_code == 403
    assert "exec" in error_text(denied)


def test_blob_upload_rejects_oversized_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_MAX_BLOB_BYTES", "32")
    app = create_app(root=tmp_path / "api-root")
    issued = app.state.api_keys.issue(tenant_id="t1", scopes={"blobs"}, actor="test")
    client = TestClient(app)
    resp = client.post(
        "/v1/blobs",
        json={"data": "x" * 64},
        headers={"X-API-Key": issued.secret},
    )
    assert resp.status_code == 413


@skip_posix_mode_bits
def test_workdir_not_world_writable_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from recertia.solver.container import ensure_workdir_writable_by_container

    monkeypatch.delenv("RECERTIA_WORKDIR_WORLD_WRITE", raising=False)
    workdir = tmp_path / "wd"
    workdir.mkdir()
    ensure_workdir_writable_by_container(workdir)
    mode = workdir.stat().st_mode & 0o777
    assert mode & 0o002 == 0  # other-write must be off
