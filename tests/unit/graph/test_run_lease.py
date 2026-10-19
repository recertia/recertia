"""Run leases are exclusive, and the fencing token lands on checkpoints."""

from __future__ import annotations

import multiprocessing as mp
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from contracts.criteria import TaskCriterion
from contracts.run import Task
from recertia.api import create_app
from recertia.graph.engine import GraphOrchestrator
from recertia.graph.lease import RunBusy, RunLease
from recertia.graph.ops import OperationLedger
from tests.support.http import error_text


def test_acquire_heartbeat_release_advances_token(tmp_path: Path) -> None:
    path = tmp_path / "locks" / "run-1.lock"
    first = RunLease(path)
    assert first.acquire() == 1
    assert first.incarnation == 1
    first.heartbeat()
    assert path.read_text(encoding="ascii").split()[0] == "1"
    first.release()

    second = RunLease(path)
    assert second.acquire() == 2
    assert second.incarnation == 1
    second.release()


def test_second_acquire_raises_run_busy(tmp_path: Path) -> None:
    path = tmp_path / "locks" / "run-1.lock"
    holder = RunLease(path)
    holder.acquire()
    try:
        with pytest.raises(RunBusy):
            RunLease(path).acquire()
    finally:
        holder.release()
    assert RunLease(path).acquire() == 2


def test_start_refuses_a_held_lease(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    holder = RunLease(runs / "locks" / "run-busy.lock")
    holder.acquire()
    orch = GraphOrchestrator(runs)
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    try:
        with pytest.raises(RunBusy):
            orch.start(
                "run-busy",
                Task(task_id="t1", request="pause", submitted_at=datetime.now(timezone.utc)),
                [
                    TaskCriterion(
                        id="output-exists",
                        kind="command",
                        run="test -f output.txt",
                        source="caller",
                        weight=1.0,
                    )
                ],
                workdir=workdir,
                max_steps=1,
            )
    finally:
        holder.release()
        orch.close()


def test_checkpoint_and_operation_store_fencing_token(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    orch = GraphOrchestrator(runs)
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    try:
        orch.start(
            "run-fenced",
            Task(task_id="t1", request="pause", submitted_at=datetime.now(timezone.utc)),
            [
                TaskCriterion(
                    id="output-exists",
                    kind="command",
                    run="test -f output.txt",
                    source="caller",
                    weight=1.0,
                )
            ],
            workdir=workdir,
            max_steps=1,
        )
        row = sqlite3.connect(runs / "runtime.db").execute(
            "SELECT fencing_token FROM checkpoints WHERE run_id=?",
            ("run-fenced",),
        ).fetchone()
        assert row is not None
        assert row[0] == 1
    finally:
        orch.close()

    ops_db = tmp_path / "operations.db"
    ops_lease = RunLease(
        tmp_path / "locks" / "ops.lock", db_path=ops_db, run_id="run-fenced"
    )
    token = ops_lease.acquire()
    ops = OperationLedger(ops_db)
    try:
        assert ops.run_once(
            "run-fenced", 1, "solve", 0, lambda: {"ok": True}, fencing_token=token
        ) == {"ok": True}
        stored = sqlite3.connect(ops_db).execute(
            "SELECT fencing_token FROM operations"
        ).fetchone()
        assert stored == (token,)
    finally:
        ops_lease.release()
        ops.close()


def test_create_run_returns_409_when_lease_is_held(tmp_path: Path) -> None:
    root = tmp_path / "api-root"
    app = create_app(root=root)
    issued = app.state.api_keys.issue(tenant_id="t1", scopes={"runs", "exec"}, actor="test")
    holder = RunLease(root / "runs" / "t1" / "locks" / "api-busy.lock")
    holder.acquire()
    try:
        client = TestClient(app)
        created = client.post(
            "/v1/runs",
            json={
                "request": "write output.txt",
                "run_id": "api-busy",
                "budget": {"max_attempts": 1},
            },
            headers={"X-API-Key": issued.secret},
        )
    finally:
        holder.release()
    assert created.status_code == 409
    assert error_text(created) == "run_busy"


def test_clean_release_keeps_incarnation_one(tmp_path: Path) -> None:
    db = tmp_path / "runtime.db"
    lock = tmp_path / "locks" / "run-1.lock"
    first = RunLease(lock, db_path=db, tenant_id="t1", run_id="run-1")
    assert first.acquire() == 1
    assert first.incarnation == 1
    first.release()
    second = RunLease(lock, db_path=db, tenant_id="t1", run_id="run-1")
    assert second.acquire() == 2
    assert second.incarnation == 1
    second.release()


def _crash_without_release(lease: RunLease) -> None:
    """Kernel-style crash: flock is freed, run_leases row stays HELD."""

    from recertia.graph.lease import _flock_unlock

    fd = lease._fd
    assert fd is not None
    lease._fd = None
    _flock_unlock(fd)
    os.close(fd)
    lease._release_thread()


def test_crash_resume_keeps_incarnation_and_does_not_rerun(tmp_path: Path) -> None:
    """CRITICAL: leftover HELD row + same lock inode is resume, not steal."""

    db = tmp_path / "runtime.db"
    lock = tmp_path / "locks" / "run-1.lock"
    calls: list[str] = []
    ops = OperationLedger(db, tenant_id="t1")
    first = RunLease(lock, db_path=db, tenant_id="t1", run_id="run-1")
    token = first.acquire()
    ops.run_once(
        "run-1",
        1,
        "solve",
        0,
        lambda: calls.append("a") or {"ok": True},
        fencing_token=token,
        incarnation=first.incarnation,
    )
    assert first.incarnation == 1
    _crash_without_release(first)

    second = RunLease(lock, db_path=db, tenant_id="t1", run_id="run-1")
    token2 = second.acquire()
    assert second.incarnation == 1
    assert token2 > token
    result = ops.run_once(
        "run-1",
        1,
        "solve",
        0,
        lambda: calls.append("b") or {"ok": False},
        fencing_token=token2,
        incarnation=second.incarnation,
    )
    assert result == {"ok": True}
    assert calls == ["a"]
    second.release()
    ops.close()


def test_incarnation_persists_after_steal_and_release(tmp_path: Path) -> None:
    db = tmp_path / "runtime.db"
    lock = tmp_path / "locks" / "run-1.lock"
    first = RunLease(lock, db_path=db, tenant_id="t1", run_id="run-1", ttl_s=0)
    first.acquire()
    assert first.incarnation == 1
    _crash_without_release(first)
    os.unlink(lock)

    thief = RunLease(lock, db_path=db, tenant_id="t1", run_id="run-1", ttl_s=0)
    thief.acquire()
    assert thief.incarnation == 2
    thief.release()

    later = RunLease(lock, db_path=db, tenant_id="t1", run_id="run-1", ttl_s=0)
    later.acquire()
    assert later.incarnation == 2
    later.release()


def _second_process_acquire(root: str, run: str, result: object) -> None:
    from recertia.graph.lease import RunBusy, RunLease

    base = Path(root)
    try:
        token = RunLease(
            base / "locks" / f"{run}.lock",
            db_path=base / "runtime.db",
            tenant_id="t1",
            run_id=run,
        ).acquire()
        result.put(("acq", token))
    except RunBusy:
        result.put(("busy", None))


def test_unlinked_lock_while_held_is_busy(tmp_path: Path) -> None:
    db = tmp_path / "runtime.db"
    lock = tmp_path / "locks" / "run-1.lock"
    holder = RunLease(lock, db_path=db, tenant_id="t1", run_id="run-1")
    holder.acquire()
    os.unlink(lock)
    ctx = mp.get_context("spawn")
    queue: mp.Queue[tuple[str, int | None]] = ctx.Queue()
    proc = ctx.Process(target=_second_process_acquire, args=(str(tmp_path), "run-1", queue))
    proc.start()
    kind, _token = queue.get(timeout=10)
    proc.join(timeout=10)
    try:
        assert kind == "busy"
    finally:
        holder.release()


def test_tokens_survive_locks_dir_wipe(tmp_path: Path) -> None:
    db = tmp_path / "runtime.db"
    lock = tmp_path / "locks" / "run-1.lock"
    tokens: list[int] = []
    for _ in range(3):
        lease = RunLease(lock, db_path=db, tenant_id="t1", run_id="run-1")
        tokens.append(lease.acquire())
        lease.release()
    shutil.rmtree(tmp_path / "locks")
    lease = RunLease(lock, db_path=db, tenant_id="t1", run_id="run-1")
    nxt = lease.acquire()
    lease.release()
    assert nxt > max(tokens)


def test_heartbeat_expiry_is_read(tmp_path: Path) -> None:
    db = tmp_path / "runtime.db"
    lock = tmp_path / "locks" / "run-1.lock"
    holder = RunLease(lock, db_path=db, tenant_id="t1", run_id="run-1", ttl_s=60)
    holder.acquire()
    _crash_without_release(holder)
    os.unlink(lock)
    busy = RunLease(lock, db_path=db, tenant_id="t1", run_id="run-1", ttl_s=60)
    with pytest.raises(RunBusy):
        busy.acquire()
    thief = RunLease(lock, db_path=db, tenant_id="t1", run_id="run-1", ttl_s=0)
    token = thief.acquire()
    assert thief.incarnation == 2
    assert token >= 2
    thief.release()
