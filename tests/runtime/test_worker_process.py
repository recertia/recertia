"""F12: queued work survives an API process that never drained it."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from recertia.api import create_app
from recertia.workers.queue import open_run_queue
from recertia.workers.serve import run_once


def test_api_restart_queued_run_still_executes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    app = create_app(root=root)
    app.state.recertia.embedded.ensure_started = lambda: None
    issued = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    client = TestClient(app)
    created = client.post(
        "/v1/runs",
        headers={"X-API-Key": issued.secret},
        json={
            "request": "survive restart",
            "task_class": "repo-chore",
            "run_id": "restart-1",
            "mode": "async",
            "budget": {"max_attempts": 1},
        },
    )
    assert created.status_code == 202, created.text
    row = app.state.recertia.queue.get("t1", "restart-1")
    assert row is not None
    assert row.status == "queued"
    app.state.recertia.queue.close()

    assert run_once(root, worker="post-restart", capacity=1) == 1
    queue = open_run_queue(root)
    try:
        finished = queue.get("t1", "restart-1")
        assert finished is not None
        assert finished.status == "succeeded"
    finally:
        queue.close()
