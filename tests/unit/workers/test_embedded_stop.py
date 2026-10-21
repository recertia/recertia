"""REC-026: EmbeddedQueueWorker.stop() really stops claiming.

test_resume_admits_quota flaked in CI because the app's embedded worker kept
polling after the create returned and claimed the test's own queue item.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, cast

from recertia.workers.embedded import EmbeddedQueueWorker
from recertia.workers.queue import QueueItem, open_run_queue


def _halt(embedded: EmbeddedQueueWorker) -> None:
    """Teardown without stop(): never close the queue under a live thread."""

    embedded._stop.set()
    if embedded._thread is not None:
        embedded._thread.join(5)


def test_stopped_worker_leaves_new_items_for_another_claimer(tmp_path: Path) -> None:
    queue = open_run_queue(tmp_path)
    embedded = EmbeddedQueueWorker(cast(Any, None), queue, worker_id="embedded")
    try:
        embedded.ensure_started()
        thread = embedded._thread
        assert thread is not None and thread.is_alive()

        embedded.stop()
        assert not thread.is_alive()

        queue.enqueue(QueueItem(tenant_id="t1", run_id="after-stop", kind="run", payload={}))
        time.sleep(0.2)  # four poll intervals
        row = queue.get("t1", "after-stop")
        assert row is not None and row.status == "queued"
        claimed = queue.claim("someone-else")
        assert [c.run_id for c in claimed] == ["after-stop"]
    finally:
        _halt(embedded)
        queue.close()


def test_stop_is_idempotent_and_ensure_started_restarts(tmp_path: Path) -> None:
    queue = open_run_queue(tmp_path)
    embedded = EmbeddedQueueWorker(cast(Any, None), queue)
    try:
        embedded.stop()  # never started: no-op
        embedded.ensure_started()
        first = embedded._thread
        embedded.stop()
        embedded.stop()
        assert first is not None and not first.is_alive()
        embedded.ensure_started()
        second = embedded._thread
        assert second is not None and second is not first and second.is_alive()
        embedded.stop()
        assert not second.is_alive()
    finally:
        _halt(embedded)
        queue.close()
