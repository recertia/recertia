"""Orphan claims return to the queue when their worker pid is gone."""

from __future__ import annotations

import os
from pathlib import Path

from recertia.workers.queue import QueueItem, open_run_queue


def test_dead_worker_claim_is_requeued(tmp_path: Path) -> None:
    queue = open_run_queue(tmp_path)
    try:
        queue.enqueue(QueueItem(tenant_id="t1", run_id="r1", kind="run", payload={}))
        claimed = queue.claim("ghost")
        assert len(claimed) == 1
        assert queue.reconcile_orphans() == 1
        row = queue.get("t1", "r1")
        assert row is not None
        assert row.status == "queued"
        assert row.attempt == 1
    finally:
        queue.close()


def test_live_worker_claim_is_kept(tmp_path: Path) -> None:
    queue = open_run_queue(tmp_path)
    try:
        queue.enqueue(QueueItem(tenant_id="t1", run_id="r1", kind="run", payload={}))
        queue.register_worker("live", capacity=1, hostname="host", pid=os.getpid())
        claimed = queue.claim("live")
        assert queue.reconcile_orphans() == 0
        row = queue.get("t1", "r1")
        assert row is not None
        assert row.status == "claimed"
        queue.finish(claimed[0], status="succeeded")
    finally:
        queue.close()


def test_exhausted_orphan_is_dead(tmp_path: Path) -> None:
    queue = open_run_queue(tmp_path)
    try:
        queue.enqueue(
            QueueItem(tenant_id="t1", run_id="r1", kind="run", payload={}, max_attempts=1)
        )
        queue.claim("ghost")
        assert queue.reconcile_orphans() == 1
        row = queue.get("t1", "r1")
        assert row is not None
        assert row.status == "dead"
        assert row.error_code == "lease_expired"
    finally:
        queue.close()


def test_request_cancel_is_visible(tmp_path: Path) -> None:
    queue = open_run_queue(tmp_path)
    try:
        queue.enqueue(QueueItem(tenant_id="t1", run_id="r1", kind="run", payload={}))
        assert queue.cancel_requested("t1", "r1") is False
        queue.request_cancel("t1", "r1")
        assert queue.cancel_requested("t1", "r1") is True
    finally:
        queue.close()
