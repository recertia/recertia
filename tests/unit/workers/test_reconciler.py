"""F12: in-flight is a query of live queue rows, not a leftover counter."""

from __future__ import annotations

from pathlib import Path

from recertia.api.quotas import QuotaStore, TenantQuota
from recertia.workers.queue import QueueItem, open_run_queue


def test_inflight_recomputed_from_queue_after_crash(tmp_path: Path) -> None:
    queue = open_run_queue(tmp_path)
    store = QuotaStore(
        tmp_path / "quotas.sqlite",
        defaults=TenantQuota(max_in_flight=1, max_runs_per_day=100),
    )
    try:
        queue.set_max_in_flight("t1", 1)
        queue.enqueue(QueueItem(tenant_id="t1", run_id="held", kind="run", payload={}))
        claimed = queue.claim("ghost")
        assert len(claimed) == 1
        assert queue.inflight("t1") == 1
        store.admit("t1")
        with store._lock, store._conn:
            store._conn.execute("UPDATE inflight SET count = 99 WHERE tenant_id = 't1'")
        assert queue.reconcile_orphans() == 1
        assert queue.inflight("t1") == 0
        store.admit(
            "t1",
            inflight=queue.inflight("t1"),
            depth=queue.depth("t1"),
            max_queue_depth=queue.max_queue_depth("t1"),
        )
    finally:
        store.close()
        queue.close()
