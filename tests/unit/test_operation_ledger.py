from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from recertia.graph.ops import _PENDING_SENTINEL, OperationLedger


def test_run_once_executes_exactly_once_across_calls(tmp_path: Path) -> None:
    ops = OperationLedger(tmp_path / "ops.db")
    calls = []

    def fn():
        calls.append(1)
        return {"value": len(calls)}

    r1 = ops.run_once("run-1", 1, "solve", 0, fn)
    r2 = ops.run_once("run-1", 1, "solve", 0, fn)  # same key: must not call fn again

    assert r1 == r2 == {"value": 1}
    assert len(calls) == 1


def test_different_keys_execute_independently(tmp_path: Path) -> None:
    ops = OperationLedger(tmp_path / "ops.db")
    calls = []

    def fn():
        calls.append(1)
        return len(calls)

    ops.run_once("run-1", 1, "solve", 0, fn)
    ops.run_once("run-1", 1, "solve", 1, fn)  # different op_seq
    ops.run_once("run-1", 2, "solve", 0, fn)  # different attempt_no
    ops.run_once("run-2", 1, "solve", 0, fn)  # different run_id

    assert len(calls) == 4


def test_survives_a_new_ledger_instance_on_the_same_db(tmp_path: Path) -> None:
    db_path = tmp_path / "ops.db"
    calls = []

    def fn():
        calls.append(1)
        return "done"

    OperationLedger(db_path).run_once("run-1", 1, "solve", 0, fn)
    # A brand-new instance (as a resumed process would create) must see the prior result.
    result = OperationLedger(db_path).run_once("run-1", 1, "solve", 0, fn)

    assert result == "done"
    assert len(calls) == 1


def test_pending_sentinel_is_not_durable_success_on_crash_resume(tmp_path: Path) -> None:
    """Crash after claim, before durable result: resume must not treat pending as done."""

    db_path = tmp_path / "ops.db"
    ops = OperationLedger(db_path, pending_timeout_s=0.05, pending_poll_s=0.01)
    calls: list[int] = []

    def crash_mid_op():
        calls.append(1)
        # Simulate process death after pending insert by leaving the sentinel in place.
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        ops.run_once("run-1", 1, "solve", 0, crash_mid_op)

    # Exception path clears the pending claim.
    applied, result = ops.get("run-1", 1, "solve", 0)
    assert applied is False
    assert result is None

    # Inject a crash-left pending row (UPDATE after claim, no clear).
    ops._conn.execute(
        "INSERT OR REPLACE INTO operations (run_id, attempt_no, node, op_seq, result_json) "
        "VALUES (?, ?, ?, ?, ?)",
        ("run-1", 1, "solve", 0, json.dumps(_PENDING_SENTINEL)),
    )
    ops._conn.commit()

    applied, result = ops.get("run-1", 1, "solve", 0)
    assert applied is False
    assert result is None

    resumed = OperationLedger(db_path, pending_timeout_s=0.05, pending_poll_s=0.01)

    def finish():
        calls.append(2)
        return {"ok": True}

    out = resumed.run_once("run-1", 1, "solve", 0, finish)
    assert out == {"ok": True}
    assert calls == [1, 2]


def test_concurrent_waiters_block_until_result_not_pending(tmp_path: Path) -> None:
    ops = OperationLedger(tmp_path / "ops.db", pending_timeout_s=2.0, pending_poll_s=0.01)
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    results: list[object] = []
    errors: list[BaseException] = []

    def slow_fn():
        calls.append("holder")
        started.set()
        assert release.wait(timeout=2.0)
        return {"value": 42}

    def must_not_run():
        raise AssertionError("waiter must not run fn")

    def waiter():
        try:
            results.append(ops.run_once("run-1", 1, "solve", 0, must_not_run))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    holder = threading.Thread(target=lambda: results.append(ops.run_once("run-1", 1, "solve", 0, slow_fn)))
    waiter_t = threading.Thread(target=waiter)
    holder.start()
    assert started.wait(timeout=2.0)
    waiter_t.start()
    time.sleep(0.05)  # waiter should be blocked on pending
    release.set()
    holder.join(timeout=2.0)
    waiter_t.join(timeout=2.0)

    assert not errors
    assert calls == ["holder"]
    assert results == [{"value": 42}, {"value": 42}]


def test_live_owner_pending_not_reclaimed_after_legacy_timeout(tmp_path: Path) -> None:
    """F7: a live in-process owner must not be reclaimed (legacy 5s timer is gone)."""

    ops = OperationLedger(tmp_path / "ops.db", pending_timeout_s=5.0, pending_poll_s=0.01)
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    results: list[object] = []
    errors: list[BaseException] = []

    def owner_a() -> dict:
        calls.append("A")
        started.set()
        assert release.wait(timeout=5.0)
        return {"owner": "A"}

    def owner_b() -> dict:
        calls.append("B")
        return {"owner": "B"}

    def run_a() -> None:
        try:
            results.append(ops.run_once("run-1", 1, "solve", 0, owner_a))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def run_b() -> None:
        try:
            results.append(ops.run_once("run-1", 1, "solve", 0, owner_b))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    holder = threading.Thread(target=run_a)
    waiter = threading.Thread(target=run_b)
    holder.start()
    assert started.wait(timeout=2.0)
    # Legacy timer would have fired at 5s; we only need to show reclaim does not
    # happen while the holder is live. Keep this short so CI cannot stall here.
    time.sleep(0.2)
    waiter.start()
    time.sleep(0.2)
    assert calls == ["A"]
    release.set()
    holder.join(timeout=2.0)
    waiter.join(timeout=2.0)

    assert not errors
    assert calls == ["A"]
    assert results == [{"owner": "A"}, {"owner": "A"}]


def test_legacy_operations_table_gains_incarnation_pk(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE operations (
            run_id TEXT NOT NULL,
            attempt_no INTEGER NOT NULL,
            node TEXT NOT NULL,
            op_seq INTEGER NOT NULL,
            result_json TEXT NOT NULL,
            PRIMARY KEY (run_id, attempt_no, node, op_seq)
        )
        """
    )
    conn.execute(
        "INSERT INTO operations VALUES ('run-1', 1, 'solve', 0, '{\"ok\": true}')"
    )
    conn.commit()
    conn.close()

    ops = OperationLedger(db)
    applied, stored = ops.get("run-1", 1, "solve", 0, incarnation=1)
    assert applied is True
    assert stored == {"ok": True}
    replay = ops.run_once("run-1", 1, "solve", 0, lambda: {"ok": False}, incarnation=1)
    assert replay == {"ok": True}


def test_new_incarnation_does_not_replay_prior_owner_ops(tmp_path: Path) -> None:
    """A stolen lease bumps incarnation, so the new owner does not reuse old op rows."""

    ops = OperationLedger(tmp_path / "ops.db")
    first = ops.run_once(
        "run-1", 1, "solve", 0, lambda: {"owner": "A"}, incarnation=1
    )
    calls: list[str] = []

    def owner_b() -> dict:
        calls.append("B")
        return {"owner": "B"}

    second = ops.run_once("run-1", 1, "solve", 0, owner_b, incarnation=2)
    assert first == {"owner": "A"}
    assert second == {"owner": "B"}
    assert calls == ["B"]
    applied, stored = ops.get("run-1", 1, "solve", 0, incarnation=1)
    assert applied is True
    assert stored == {"owner": "A"}
