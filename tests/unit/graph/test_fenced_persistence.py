"""REL-3: checkpoint writes fail when the fencing token is no longer current."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.criteria import TaskCriterion
from contracts.run import RunManifest, RunState, Task
from recertia.graph import lease as lease_mod
from recertia.graph.lease import RunLease
from recertia.graph.ops import OperationLedger
from recertia.graph.persistence import FencedRunPersistence, open_run_persistence
from recertia.graph.store import CheckpointStore
from recertia.ledger import HashChainLedger
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
    # Crash without deleting the row (same lock file → resume, not steal).
    from recertia.graph.lease import _flock_unlock

    fd = holder._fd
    assert fd is not None
    holder._fd = None
    _flock_unlock(fd)
    os.close(fd)
    holder._release_thread()

    thief = RunLease(lock, db_path=db, run_id="run-1")
    assert thief.acquire() == 3
    # Crash left a HELD row on the same lock file: resume, not steal.
    assert thief.incarnation == 1

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


def test_stale_token_rejects_operation(tmp_path: Path) -> None:
    """D2: OperationLedger must not persist a write with a stale fencing token."""

    db = tmp_path / "runtime.db"
    lock = tmp_path / "locks" / "r4.lock"
    holder = RunLease(lock, db_path=db, run_id="r4")
    stale = holder.acquire()
    holder.release()
    current = RunLease(lock, db_path=db, run_id="r4")
    token = current.acquire()
    assert token != stale
    ops = OperationLedger(db)
    with pytest.raises(LeaseLost):
        ops.run_once("r4", 1, "solve", 7, lambda: {"stale": True}, fencing_token=stale)
    applied, _result = ops.get("r4", 1, "solve", 7)
    assert applied is False
    assert ops.run_once("r4", 1, "solve", 7, lambda: {"ok": True}, fencing_token=token) == {
        "ok": True
    }
    current.release()
    ops.close()


def test_held_run_ledger_append_requires_token(tmp_path: Path) -> None:
    """D3: hash-chain append targeting a held run_id is rejected without a token."""

    root = tmp_path
    lock = root / "locks" / "r4.lock"
    db = root / "runtime.db"
    holder = RunLease(lock, db_path=db, run_id="r4")
    token = holder.acquire()
    ledger = HashChainLedger(root / "ledger.jsonl")
    with pytest.raises(LeaseLost):
        ledger.append(actor="stale", action="write", target="r4")
    entry = ledger.append(actor="holder", action="write", target="r4", fencing_token=token)
    assert entry.target == "r4"
    skill = HashChainLedger(root / "other.jsonl")
    skill.append(actor="ci", action="write", target="skill-1")
    holder.release()


def test_check_then_write_is_atomic(tmp_path: Path) -> None:
    """E: a steal between require_lease_token and INSERT must not land the write."""

    root = tmp_path
    a = RunLease(root / "locks" / "r5.lock", db_path=root / "runtime.db", run_id="r5")
    token = a.acquire()
    store = CheckpointStore(root / "runtime.db", lease_db=root / "runtime.db", tenant_id="local")
    real = lease_mod.require_lease_token

    def check_then_steal(*args: object, **kwargs: object) -> None:
        real(*args, **kwargs)
        conn = sqlite3.connect(root / "runtime.db")
        conn.execute(
            "UPDATE run_leases SET fencing_token=fencing_token+1, incarnation=incarnation+1"
        )
        conn.commit()
        conn.close()

    lease_mod.require_lease_token = check_then_steal  # type: ignore[method-assign]
    try:
        with pytest.raises(LeaseLost):
            store.save("r5", 1, "n", None, _state("r5"), fencing_token=token)
    finally:
        lease_mod.require_lease_token = real  # type: ignore[method-assign]
    assert store.latest("r5") is None
    a.release()
    store.close()


def test_missing_lease_db_rejected(tmp_path: Path) -> None:
    """G: a fencing write with no runtime.db must fail closed."""

    store = CheckpointStore(tmp_path / "ck.db", lease_db=tmp_path / "missing-runtime.db")
    with pytest.raises(LeaseLost):
        store.save("r7", 1, "n", None, _state("r7"), fencing_token=999)
    assert store.latest("r7") is None
    store.close()


def test_two_file_check_then_write_is_atomic(tmp_path: Path) -> None:
    """Same as E when checkpoints.db and runtime.db are distinct files (ATTACH)."""

    runs = tmp_path / "runs"
    db = runs / "runtime.db"
    lock = runs / "locks" / "r5.lock"
    a = RunLease(lock, db_path=db, run_id="r5")
    token = a.acquire()
    store = CheckpointStore(runs / "checkpoints.db", lease_db=db, tenant_id="local")
    real = lease_mod.require_lease_token

    def check_then_steal(*args: object, **kwargs: object) -> None:
        real(*args, **kwargs)
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE run_leases SET fencing_token=fencing_token+1, incarnation=incarnation+1"
        )
        conn.commit()
        conn.close()

    lease_mod.require_lease_token = check_then_steal  # type: ignore[method-assign]
    try:
        with pytest.raises(LeaseLost):
            store.save("r5", 1, "n", None, _state("r5"), fencing_token=token)
    finally:
        lease_mod.require_lease_token = real  # type: ignore[method-assign]
    assert store.latest("r5") is None
    a.release()
    store.close()
