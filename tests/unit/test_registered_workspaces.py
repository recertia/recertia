"""RW-* conformance: registered host workspaces (Pilot workdir bind)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from recertia.api import create_app
from recertia.paths import looks_absolute, normalize_host_root, resolve_under_host_root, split_rel_subpath
from tests.support.http import error_text


def _proven_output_criterion() -> dict:
    return {
        "id": "output-exists",
        "kind": "command",
        "run": "test -f output.txt",
        "source": "caller",
        "weight": 1.0,
        "sensitivity_proof": {
            "criterion_id": "output-exists",
            "negative_fixture": "empty workspace",
            "rejected": True,
            "checked_at": "2026-01-01T00:00:00Z",
        },
    }


def _client(tmp_path: Path, *, scopes: set[str] | None = None):
    app = create_app(root=tmp_path / "api-root")
    scopes = scopes or {"runs", "exec", "admin", "platform_admin"}
    issued = app.state.api_keys.issue(tenant_id="t1", scopes=scopes, actor="test")
    return app, TestClient(app), {"X-API-Key": issued.secret}


def test_rw1_absolute_workdir_without_workspace_still_rejected(tmp_path: Path) -> None:
    _, client, headers = _client(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    res = client.post(
        "/v1/runs",
        json={
            "request": "x",
            "run_id": "abs-wd",
            "workdir": str(outside),
            "budget": {"max_attempts": 1},
        },
        headers=headers,
    )
    assert res.status_code == 400
    assert "absolute" in error_text(res).lower()


def test_rw2_register_and_bind_run(tmp_path: Path) -> None:
    app, client, headers = _client(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    reg = client.post(
        "/v1/workspaces",
        json={
            "workspace_id": "recertia",
            "display_name": "test repo",
            "host_root": str(repo),
        },
        headers=headers,
    )
    assert reg.status_code == 201, reg.text
    body = reg.json()
    assert body["workspace_id"] == "recertia"
    assert Path(body["host_root"]) == repo.resolve()

    created = client.post(
        "/v1/runs",
        json={
            "request": "write output.txt",
            "run_id": "bind-run",
            "workspace_id": "recertia",
            "script": ["python3 -c \"open('output.txt','w').write('done')\""],
            "criteria": [_proven_output_criterion()],
        },
        headers=headers,
    )
    assert created.status_code == 200, created.text
    assert (repo / "output.txt").read_text() == "done"
    assert created.json()["workspace_id"] == "recertia"
    meta = json.loads(
        (tmp_path / "api-root" / "runs" / "t1" / "bind-run" / "workdir.json").read_text()
    )
    assert meta["kind"] == "registered"
    assert meta["workspace_id"] == "recertia"


def test_rw3_subpath_escape_rejected(tmp_path: Path) -> None:
    _, client, headers = _client(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    client.post(
        "/v1/workspaces",
        json={"workspace_id": "w1", "display_name": "r", "host_root": str(repo)},
        headers=headers,
    )
    res = client.post(
        "/v1/runs",
        json={
            "request": "x",
            "run_id": "esc",
            "workspace_id": "w1",
            "workdir": "../other",
            "budget": {"max_attempts": 1},
        },
        headers=headers,
    )
    assert res.status_code == 400
    assert "escape" in error_text(res).lower()


def test_rw4_cross_tenant_isolation(tmp_path: Path) -> None:
    app, client, headers = _client(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    client.post(
        "/v1/workspaces",
        json={"workspace_id": "secret", "display_name": "r", "host_root": str(repo)},
        headers=headers,
    )
    other = app.state.api_keys.issue(tenant_id="t2", scopes={"runs", "admin"}, actor="test")
    other_h = {"X-API-Key": other.secret}
    listed = client.get("/v1/workspaces", headers=other_h)
    assert listed.status_code == 200
    assert listed.json()["workspaces"] == []
    got = client.get("/v1/workspaces/secret", headers=other_h)
    assert got.status_code == 404
    bind = client.post(
        "/v1/runs",
        json={"request": "x", "workspace_id": "secret", "budget": {"max_attempts": 1}},
        headers=other_h,
    )
    assert bind.status_code == 404


def test_rw5_disabled_workspace_blocks_create(tmp_path: Path) -> None:
    _, client, headers = _client(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    client.post(
        "/v1/workspaces",
        json={"workspace_id": "w1", "display_name": "r", "host_root": str(repo)},
        headers=headers,
    )
    disabled = client.delete("/v1/workspaces/w1", headers=headers)
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    res = client.post(
        "/v1/runs",
        json={"request": "x", "workspace_id": "w1", "budget": {"max_attempts": 1}},
        headers=headers,
    )
    assert res.status_code == 403
    assert "disabled" in error_text(res).lower()


def test_rw6_resume_registered_and_host_root_drift(tmp_path: Path) -> None:
    app, client, headers = _client(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    client.post(
        "/v1/workspaces",
        json={"workspace_id": "w1", "display_name": "r", "host_root": str(repo)},
        headers=headers,
    )
    created = client.post(
        "/v1/runs",
        json={
            "request": "write output.txt",
            "run_id": "resume-reg",
            "workspace_id": "w1",
            "script": ["python3 -c \"open('output.txt','w').write('a')\""],
            "criteria": [_proven_output_criterion()],
            "budget": {"max_attempts": 1},
        },
        headers=headers,
    )
    assert created.status_code == 200, created.text

    # Tamper stored host_root to force drift detection on resume.
    meta_path = tmp_path / "api-root" / "runs" / "t1" / "resume-reg" / "workdir.json"
    meta = json.loads(meta_path.read_text())
    meta["host_root"] = str(tmp_path / "other-root")
    meta_path.write_text(json.dumps(meta) + "\n")
    resumed = client.post("/v1/runs/resume-reg/resume", headers=headers)
    assert resumed.status_code == 409
    assert "host_root" in error_text(resumed).lower()


def test_rw7_pilot_submit_body_builder_includes_workspace() -> None:
    """Mirror console submit payload builder (RW-7)."""

    def build_submit_body(*, workspace_id: str, subpath: str, goal: dict) -> dict:
        body: dict = {
            "goal": goal,
            "task_class": goal.get("task_class") or "repo-chore",
            "mode": "sync",
            "budget": {"max_attempts": 2},
        }
        if workspace_id:
            body["workspace_id"] = workspace_id
            body["workdir"] = subpath or ""
        return body

    goal = {"goal_id": "g", "desired": [], "constraints": [], "task_class": "repo-chore"}
    body = build_submit_body(workspace_id="recertia", subpath="", goal=goal)
    assert body["workspace_id"] == "recertia"
    assert body["workdir"] == ""
    sandbox = build_submit_body(workspace_id="", subpath="", goal=goal)
    assert "workspace_id" not in sandbox


def test_rw8_non_admin_cannot_register(tmp_path: Path) -> None:
    app = create_app(root=tmp_path / "api-root")
    issued = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    client = TestClient(app)
    repo = tmp_path / "repo"
    repo.mkdir()
    res = client.post(
        "/v1/workspaces",
        json={"workspace_id": "w1", "display_name": "r", "host_root": str(repo)},
        headers={"X-API-Key": issued.secret},
    )
    assert res.status_code == 403
    assert "admin" in error_text(res).lower()


def test_rw9_mixed_separators_resolve_identically(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    nested = root / "subdir" / "foo"
    nested.mkdir(parents=True)
    a = resolve_under_host_root(str(root), "subdir/foo")
    b = resolve_under_host_root(str(root), "subdir\\foo")
    assert a == b == nested.resolve()
    assert split_rel_subpath("subdir/foo") == ("subdir", "foo")
    assert split_rel_subpath("subdir\\foo") == ("subdir", "foo")


def test_normalize_host_root_posix_escape(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    stored = normalize_host_root(str(root))
    assert Path(stored) == root.resolve()
    assert looks_absolute(str(root))


def test_duplicate_workspace_id_conflict(tmp_path: Path) -> None:
    _, client, headers = _client(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    first = client.post(
        "/v1/workspaces",
        json={"workspace_id": "w1", "display_name": "r", "host_root": str(repo)},
        headers=headers,
    )
    assert first.status_code == 201
    second = client.post(
        "/v1/workspaces",
        json={"workspace_id": "w1", "display_name": "r2", "host_root": str(repo)},
        headers=headers,
    )
    assert second.status_code == 409
