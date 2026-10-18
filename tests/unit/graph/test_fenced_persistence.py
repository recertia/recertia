"""REL-3: checkpoint writes fail when the fencing token is no longer current."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.criteria import TaskCriterion
from contracts.run import RunManifest, RunState, Task
from recertia.graph.lease import RunLease
from recertia.graph.persistence import FencedRunPersistence, open_run_persistence
from recertia.graph.store import CheckpointStore
from recertia.runtime.errors import LeaseLost


def _state(run_id: str) -> RunState:
    return RunState(
        run_id=run_id,
        task=Task(
            task_id=run_id,
            request="x",
            task_class="repo-chore",
            submitted_at=datetime.now(timezone.utc),
        ),
        manifest=RunManifest(index_snapshot_id="snap", criteria_hash="h"),
        criteria=[TaskCriterion(id="c", kind="command", run="true", source="caller")],
        criteria_locked_at=datetime.now(timezone.utc),
    )


def test_stale_fencing_token_is_lease_lost(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    lock = runs / "locks" / "run-1.lock"
    db = runs / "runtime.db"
    first = RunLease(lock, db_path=db, run_id="run-1")
    assert first.acquire() == 1
    first.release()

    holder = RunLease(lock, db_path=db, run_id="run-1")
    assert holder.acquire() == 2
    # Crash without deleting the row, then steal.
    from recertia.graph.lease import _flock_unlock

    fd = holder._fd
    assert fd is not None
    holder._fd = None
    _flock_unlock(fd)
    os.close(fd)
    holder._release_thread()

    thief = RunLease(lock, db_path=db, run_id="run-1")
    assert thief.acquire() == 3
    assert thief.incarnation == 2

    store = CheckpointStore(runs / "checkpoints.db", lease_db=db, tenant_id="local")
    with pytest.raises(LeaseLost):
        store.save("run-1", 0, "intake", "retrieve", _state("run-1"), fencing_token=1)
    store.save("run-1", 0, "intake", "retrieve", _state("run-1"), fencing_token=3)
    thief.release()
    store.close()


def test_fenced_wrapper_rejects_stale_token(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    inner = open_run_persistence(runs)
    fenced = FencedRunPersistence(inner, runtime_db=runs / "runtime.db")
    lock = runs / "locks" / "run-2.lock"
    lease = RunLease(lock, db_path=runs / "runtime.db", run_id="run-2")
    token = lease.acquire()
    fenced.save_checkpoint("run-2", 0, "intake", "retrieve", _state("run-2"), fencing_token=token)
    with pytest.raises(LeaseLost):
        fenced.save_checkpoint("run-2", 1, "retrieve", "plan", _state("run-2"), fencing_token=token + 99)
    lease.release()
    fenced.close()
