"""Optional wait on mode=async. The default async flag stays off."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from recertia.api import create_app
from recertia.settings import load_settings


def _client(tmp_path: Path) -> tuple[TestClient, str, object]:
    app = create_app(root=tmp_path / "root")
    issued = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    return TestClient(app), issued.secret, app


def test_default_async_flag_stays_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECERTIA_API_DEFAULT_ASYNC", raising=False)
    assert load_settings().api_default_async is False


def test_async_wait_returns_the_finished_run(tmp_path: Path) -> None:
    client, secret, _app = _client(tmp_path)
    created = client.post(
        "/v1/runs",
        params={"wait": "30"},
        headers={"X-API-Key": secret},
        json={
            "request": "async chore",
            "task_class": "repo-chore",
            "run_id": "wait-done",
            "mode": "async",
            "budget": {"max_attempts": 1},
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["run_id"] == "wait-done"
    assert body["terminal"] in {"solved", "unsolved", "abstained"}
    assert body["mode"] == "async"


def test_async_wait_timeout_returns_202(tmp_path: Path) -> None:
    client, secret, app = _client(tmp_path)
    app.state.console_ctx.embedded.ensure_started = lambda: None
    created = client.post(
        "/v1/runs",
        headers={"X-API-Key": secret, "X-Recertia-Wait": "0.05"},
        json={
            "request": "async chore",
            "task_class": "repo-chore",
            "run_id": "wait-slow",
            "mode": "async",
            "budget": {"max_attempts": 1},
        },
    )
    assert created.status_code == 202, created.text
    assert created.json()["status"] == "queued"
    row = app.state.console_ctx.queue.get("t1", "wait-slow")
    assert row is not None
    assert row.status == "queued"
