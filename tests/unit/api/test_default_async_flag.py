"""D4: default async stays off; flipping it returns 202 when mode is omitted."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from recertia.api import create_app
from recertia.settings import load_settings


def test_default_off_waits_and_default_on_returns_202(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RECERTIA_API_DEFAULT_ASYNC", raising=False)
    assert load_settings().api_default_async is False
    off_app = create_app(root=tmp_path / "off")
    issued = off_app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    off_client = TestClient(off_app)
    waited = off_client.post(
        "/v1/runs",
        headers={"X-API-Key": issued.secret},
        json={
            "request": "default wait",
            "task_class": "repo-chore",
            "run_id": "default-off",
            "budget": {"max_attempts": 1},
        },
    )
    assert waited.status_code == 200, waited.text
    assert waited.json()["terminal"] in {"solved", "unsolved", "abstained"}

    monkeypatch.setenv("RECERTIA_API_DEFAULT_ASYNC", "1")
    on_app = create_app(root=tmp_path / "on")
    on_issued = on_app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    on_client = TestClient(on_app)
    accepted = on_client.post(
        "/v1/runs",
        headers={"X-API-Key": on_issued.secret},
        json={
            "request": "default async",
            "task_class": "repo-chore",
            "run_id": "default-on",
            "budget": {"max_attempts": 1},
        },
    )
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["status"] == "queued"
    on_app.state.recertia.embedded.flush(timeout=30)

    sync = on_client.post(
        "/v1/runs",
        headers={"X-API-Key": on_issued.secret},
        json={
            "request": "force sync",
            "task_class": "repo-chore",
            "run_id": "default-on-sync",
            "mode": "sync",
            "budget": {"max_attempts": 1},
        },
    )
    assert sync.status_code == 200, sync.text
    assert sync.json()["terminal"] in {"solved", "unsolved", "abstained"}
