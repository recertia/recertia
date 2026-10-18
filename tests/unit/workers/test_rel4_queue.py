"""REL-4 regression: restart keeps queued runs; cancel is honored."""

from __future__ import annotations

from pathlib import Path

from recertia.workers.queue import QueueItem, open_run_queue


def test_restart_keeps_queued_runs(tmp_path: Path) -> None:
    root = tmp_path / "root"
    queue = open_run_queue(root)
    try:
        queue.enqueue(QueueItem(tenant_id="t1", run_id="r-survive", kind="run", payload={"n": 1}))
        assert queue.depth("t1") == 1
    finally:
        queue.close()

    # Simulate process restart by reopening.
    queue2 = open_run_queue(root)
    try:
        row = queue2.get("t1", "r-survive")
        assert row is not None
        assert row.status == "queued"
        claimed = queue2.claim("worker-restart")
        assert len(claimed) == 1
        assert claimed[0].run_id == "r-survive"
        queue2.finish(claimed[0], status="succeeded")
    finally:
        queue2.close()


def test_request_cancel_is_visible(tmp_path: Path) -> None:
    queue = open_run_queue(tmp_path)
    try:
        queue.enqueue(QueueItem(tenant_id="t1", run_id="r-cancel", kind="run", payload={}))
        queue.request_cancel("t1", "r-cancel")
        assert queue.cancel_requested("t1", "r-cancel") is True
        claimed = queue.claim("worker-a")
        assert len(claimed) == 1
        # Worker should observe cancel and finish cancelled.
        queue.finish(claimed[0], status="cancelled")
        row = queue.get("t1", "r-cancel")
        assert row is not None
        assert row.status == "cancelled"
    finally:
        queue.close()
