"""mode=async is durable on run_queue. mode=sync stays on the request thread."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from recertia.api import create_app


def test_async_run_is_finished_by_the_queue_worker(tmp_path: Path) -> None:
    app = create_app(root=tmp_path / "root")
    issued = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    client = TestClient(app)
    created = client.post(
        "/v1/runs",
        headers={"X-API-Key": issued.secret},
        json={
            "request": "async chore",
            "task_class": "repo-chore",
            "run_id": "async-q",
            "mode": "async",
            "budget": {"max_attempts": 1},
        },
    )
    assert created.status_code == 202, created.text
    assert created.json()["status"] == "queued"
    app.state.recertia.embedded.flush(timeout=30)
    row = app.state.recertia.queue.get("t1", "async-q")
    assert row is not None
    assert row.status == "succeeded"
    got = client.get("/v1/runs/async-q", headers={"X-API-Key": issued.secret})
    assert got.status_code == 200
    assert got.json()["terminal"] in {"solved", "unsolved", "abstained"}
