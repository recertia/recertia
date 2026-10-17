"""Deficit-round-robin claim and the worker CLI."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from typer.testing import CliRunner

from recertia.cli.main import app
from recertia.workers.queue import QueueItem, open_run_queue

runner = CliRunner()


def _stamp(index: int) -> str:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return (base + timedelta(microseconds=index)).isoformat()


def test_flooding_tenant_does_not_starve_small_tenant(tmp_path: Path) -> None:
    queue = open_run_queue(tmp_path)
    try:
        for index in range(100):
            queue.enqueue(
                QueueItem(
                    tenant_id="flood",
                    run_id=f"a-{index}",
                    kind="run",
                    payload={"n": index},
                    enqueued_at=_stamp(index),
                )
            )
        queue.enqueue(
            QueueItem(
                tenant_id="quiet",
                run_id="b-0",
                kind="run",
                payload={"n": 0},
                enqueued_at=_stamp(1000),
            )
        )
        queue.set_max_in_flight("flood", 1)
        queue.set_max_in_flight("quiet", 1)
        assert queue.depth("flood") == 100
        assert queue.depth("quiet") == 1

        order: list[str] = []
        for _ in range(10):
            claims = queue.claim("worker-a", limit=1)
            assert len(claims) == 1
            order.append(claims[0].tenant_id)
            assert queue.inflight(claims[0].tenant_id) == 1
            queue.finish(claims[0], status="succeeded")
            assert queue.inflight(claims[0].tenant_id) == 0
    finally:
        queue.close()

    assert order[0] == "flood"
    assert "quiet" in order
    assert order.index("quiet") < 10


def test_frozen_queue_claims_nothing(tmp_path: Path) -> None:
    queue = open_run_queue(tmp_path)
    try:
        queue.enqueue(QueueItem(tenant_id="t1", run_id="r1", kind="run", payload={}))
        queue.set_maintenance("frozen")
        assert queue.claim("worker-a") == []
        queue.set_maintenance("open")
        claimed = queue.claim("worker-a")
        assert len(claimed) == 1
        assert claimed[0].claim_token == 1
    finally:
        queue.close()


def test_worker_once_runs_queued_item(tmp_path: Path) -> None:
    root = tmp_path / "root"
    workdir = root / "ws"
    workdir.mkdir(parents=True)
    queue = open_run_queue(root)
    queue.enqueue(
        QueueItem(
            tenant_id="t1",
            run_id="w1",
            kind="run",
            payload={
                "request": "pause",
                "max_steps": 1,
                "workdir": str(workdir),
                "approve_tools": False,
                "criteria": [
                    {
                        "id": "output-exists",
                        "kind": "command",
                        "run": "test -f output.txt",
                        "source": "caller",
                        "weight": 1.0,
                    }
                ],
            },
        )
    )
    queue.close()

    result = runner.invoke(app, ["worker", "--root", str(root), "--once"])
    assert result.exit_code == 0, result.output
    assert "claimed=1" in result.output

    queue = open_run_queue(root)
    try:
        row = queue.get("t1", "w1")
        assert row is not None
        assert row.status == "succeeded"
    finally:
        queue.close()

    empty = runner.invoke(app, ["worker", "--root", str(root), "--once"])
    assert empty.exit_code == 0, empty.output
    assert "claimed=0" in empty.output
