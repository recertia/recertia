"""mode=async is durable on run_queue. mode=sync enqueue-and-waits on the same table."""

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


def test_embedded_queue_worker_stop_joins_thread(tmp_path: Path) -> None:
    from recertia.api.events import RunEventLog
    from recertia.workers.embedded import EmbeddedQueueWorker
    from recertia.workers.queue import open_run_queue
    from recertia.workers.run_worker import AsyncRunWorker

    queue = open_run_queue(tmp_path / "root")
    worker = AsyncRunWorker(events=RunEventLog(tmp_path / "root"))
    worker.attach_queue(queue)
    embedded = EmbeddedQueueWorker(worker, queue, worker_id="stop-test")
    embedded.ensure_started()
    thread = embedded._thread
    assert thread is not None and thread.is_alive()
    embedded.stop(timeout=5)
    assert not thread.is_alive()
    embedded.stop(timeout=5)
