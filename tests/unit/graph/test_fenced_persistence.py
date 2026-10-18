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


def test_stale_ledger_token_rejected_after_steal_and_release(tmp_path: Path) -> None:
    """89-A: after steal+release, stale and token-less hash-chain appends fail closed."""

    root = tmp_path
    lock = root / "locks" / "r-steal.lock"
    db = root / "runtime.db"
    holder = RunLease(lock, db_path=db, run_id="r-steal")
    stale = holder.acquire()
    holder.release()
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE run_leases SET fencing_token=fencing_token+1, incarnation=incarnation+1, "
        "status='released'"
    )
    conn.commit()
    highest = conn.execute("SELECT fencing_token FROM run_leases").fetchone()[0]
    conn.close()
    assert highest == stale + 1
    ledger = HashChainLedger(root / "ledger.jsonl", lease_db=db)
    with pytest.raises(LeaseLost):
        ledger.append(actor="stale", action="write", target="r-steal", fencing_token=stale)
    with pytest.raises(LeaseLost):
        ledger.append(actor="stale", action="write", target="r-steal")
    with pytest.raises(LeaseLost):
        ledger.append(actor="released", action="write", target="r-steal", fencing_token=highest)
    skill = HashChainLedger(root / "other.jsonl", lease_db=db)
    skill.append(actor="ci", action="write", target="skill-1")


def test_released_run_tokenless_writes_rejected(tmp_path: Path) -> None:
    """P3 ruling: token-less writes are allowed only on a never-leased run."""

    root = tmp_path
    persist = open_run_persistence(root)
    lock = root / "locks" / "done.lock"
    db = root / "runtime.db"
    holder = RunLease(lock, db_path=db, run_id="done")
    token = holder.acquire()
    persist.checkpoints.save("done", 1, "n", None, _state("done"), fencing_token=token)
    holder.release()
    with pytest.raises(LeaseLost):
        persist.ledger.append(actor="reviewer", action="write", target="done")
    with pytest.raises(LeaseLost):
        persist.ledger.append(actor="old", action="write", target="done", fencing_token=token)
    with pytest.raises(LeaseLost):
        persist.checkpoints.save("done", 2, "n", None, _state("done"))
    with pytest.raises(LeaseLost):
        persist.operations.run_once("done", 1, "solve", 0, lambda: {"v": 1})
    persist.close()


def test_never_leased_tokenless_writes_allowed(tmp_path: Path) -> None:
    persist = open_run_persistence(tmp_path)
    persist.checkpoints.save("fresh", 1, "n", None, _state("fresh"))
    persist.operations.run_once("fresh", 1, "solve", 0, lambda: {"v": 1})
    entry = persist.ledger.append(actor="x", action="write", target="fresh")
    assert entry.target == "fresh"
    persist.close()


def test_hashchain_append_holds_lease_lock_until_write(tmp_path: Path) -> None:
    """Steal cannot mutate run_leases while a fenced append is in its write txn."""

    root = tmp_path
    persist = open_run_persistence(root)
    lock = RunLease(root / "locks" / "r-hc.lock", db_path=root / "runtime.db", run_id="r-hc")
    token = lock.acquire()
    stolen: list[str] = []
    real = HashChainLedger._append_locked

    def during_write(self: HashChainLedger, **kwargs: object) -> object:
        conn = sqlite3.connect(str(root / "runtime.db"), timeout=0.2)
        try:
            conn.execute("PRAGMA busy_timeout=200")
            conn.execute("UPDATE run_leases SET fencing_token = fencing_token + 1")
            conn.commit()
            stolen.append("updated")
        except sqlite3.OperationalError:
            stolen.append("blocked")
        finally:
            conn.close()
        return real(self, **kwargs)

    HashChainLedger._append_locked = during_write  # type: ignore[method-assign]
    try:
        entry = persist.ledger.append(
            actor="holder", action="write", target="r-hc", fencing_token=token
        )
    finally:
        HashChainLedger._append_locked = real  # type: ignore[method-assign]
        persist.close()
        lock.release()
    assert stolen == ["blocked"]
    assert entry.target == "r-hc"


def test_tokenless_held_run_writes_rejected(tmp_path: Path) -> None:
    """89-B: checkpoint, operations, and ledger require a token on a held run."""

    root = tmp_path
    persist = open_run_persistence(root)
    lock = root / "locks" / "r-held.lock"
    db = root / "runtime.db"
    holder = RunLease(lock, db_path=db, run_id="r-held")
    token = holder.acquire()
    try:
        with pytest.raises(LeaseLost):
            persist.checkpoints.save("r-held", 1, "n", None, _state("r-held"))
        assert persist.checkpoints.latest("r-held") is None
        effect: list[int] = []
        with pytest.raises(LeaseLost):
            persist.operations.run_once(
                "r-held", 1, "solve", 0, lambda: effect.append(1) or {"stale": True}
            )
        assert effect == []
        with pytest.raises(LeaseLost):
            persist.ledger.append(actor="stale", action="write", target="r-held")
        persist.checkpoints.save(
            "r-held", 1, "n", None, _state("r-held"), fencing_token=token
        )
    finally:
        holder.release()
        persist.close()


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
