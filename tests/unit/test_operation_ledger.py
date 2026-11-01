from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from recertia.graph.ops import _PENDING_SENTINEL, OperationConflict, OperationLedger
from recertia.persistence.root_lock import RootBusy, RootLock


def test_run_once_executes_exactly_once_across_calls(tmp_path: Path) -> None:
    with closing(OperationLedger(tmp_path / "ops.db")) as ops:
        calls = []

        def fn():
            calls.append(1)
            return {"value": len(calls)}

        r1 = ops.run_once("run-1", 1, "solve", 0, fn)
        r2 = ops.run_once("run-1", 1, "solve", 0, fn)  # same key: must not call fn again

        assert r1 == r2 == {"value": 1}
        assert len(calls) == 1


def test_different_keys_execute_independently(tmp_path: Path) -> None:
    with closing(OperationLedger(tmp_path / "ops.db")) as ops:
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

    with closing(OperationLedger(db_path)) as first:
        first.run_once("run-1", 1, "solve", 0, fn)
    # A brand-new instance (as a resumed process would create) must see the prior result.
    with closing(OperationLedger(db_path)) as second:
        result = second.run_once("run-1", 1, "solve", 0, fn)

    assert result == "done"
    assert len(calls) == 1


def test_pending_sentinel_is_not_durable_success_on_crash_resume(tmp_path: Path) -> None:
    """Crash after claim, before durable result: resume must not treat pending as done."""

    db_path = tmp_path / "ops.db"
    with closing(OperationLedger(db_path, pending_timeout_s=0.05, pending_poll_s=0.01)) as ops:
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

        with closing(OperationLedger(db_path, pending_timeout_s=0.05, pending_poll_s=0.01)) as resumed:

            def finish():
                calls.append(2)
                return {"ok": True}

            out = resumed.run_once("run-1", 1, "solve", 0, finish)
            assert out == {"ok": True}
            assert calls == [1, 2]


def test_concurrent_waiters_block_until_result_not_pending(tmp_path: Path) -> None:
    with closing(OperationLedger(tmp_path / "ops.db", pending_timeout_s=2.0, pending_poll_s=0.01)) as ops:
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

        holder = threading.Thread(
            target=lambda: results.append(ops.run_once("run-1", 1, "solve", 0, slow_fn))
        )
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

    with closing(OperationLedger(tmp_path / "ops.db", pending_timeout_s=5.0, pending_poll_s=0.01)) as ops:
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

    with closing(OperationLedger(db)) as ops:
        applied, stored = ops.get("run-1", 1, "solve", 0, incarnation=1)
        assert applied is True
        assert stored == {"ok": True}
        replay = ops.run_once("run-1", 1, "solve", 0, lambda: {"ok": False}, incarnation=1)
        assert replay == {"ok": True}


def test_new_incarnation_does_not_replay_prior_owner_ops(tmp_path: Path) -> None:
    """A stolen lease bumps incarnation, so the new owner does not reuse old op rows."""

    with closing(OperationLedger(tmp_path / "ops.db")) as ops:
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


def test_duplicate_put_raises_and_does_not_overwrite(tmp_path: Path) -> None:
    """put() is write-once. Overwrite would hide a double-applied side effect."""

    with closing(OperationLedger(tmp_path / "ops.db")) as ops:
        ops.put("run-1", 1, "solve", 0, {"value": 1})
        with pytest.raises(OperationConflict, match="already exists"):
            ops.put("run-1", 1, "solve", 0, {"value": 2})
        applied, stored = ops.get("run-1", 1, "solve", 0)
        assert applied is True
        assert stored == {"value": 1}


def test_ops_rebuild_resumable_after_operations_rel3_exists(tmp_path: Path) -> None:
    """Crash after CREATE operations_rel3 must not fail every later open."""

    db = tmp_path / "operations.db"
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
    conn.execute("INSERT INTO operations VALUES ('old', 1, 'n', 0, '{\"v\": 1}')")
    conn.commit()
    from recertia.graph import ops as ops_mod

    conn.execute(ops_mod._OPERATIONS_DDL.replace("operations", "operations_rel3", 1))
    conn.commit()
    conn.close()

    with closing(OperationLedger(db)) as ops:
        applied, stored = ops.get("old", 1, "n", 0)
        assert applied is True
        assert stored == {"v": 1}
        with closing(sqlite3.connect(db)) as conn:
            leftover = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='operations_rel3'"
            ).fetchone()
        assert leftover is None


def test_ops_rebuild_resumable_after_drop_before_rename(tmp_path: Path) -> None:
    db = tmp_path / "operations.db"
    conn = sqlite3.connect(db)
    from recertia.graph import ops as ops_mod

    conn.execute(ops_mod._OPERATIONS_DDL.replace("operations", "operations_rel3", 1))
    conn.execute(
        "INSERT INTO operations_rel3 "
        "(run_id, incarnation, attempt_no, node, op_seq, result_json) "
        "VALUES ('old', 1, 1, 'n', 0, '{\"v\": 2}')"
    )
    conn.commit()
    conn.close()

    with closing(OperationLedger(db)) as ops:
        applied, stored = ops.get("old", 1, "n", 0)
        assert applied is True
        assert stored == {"v": 2}


# Bounded-wait knob for the lock tests: the refusal must come after about this long. The
# join margin only guards against a regression that blocks forever, so it is generous for
# slow runners.
_LOCK_WAIT_S = 0.3
_JOIN_MARGIN_S = 10.0


def _make_legacy_operations_db(db: Path) -> None:
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
    conn.execute("INSERT INTO operations VALUES ('old', 1, 'n', 0, '{\"v\": 1}')")
    conn.commit()
    conn.close()


def _db_snapshot(db: Path) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    """Schema objects and legacy ``operations`` rows, to prove a refused open wrote nothing."""

    conn = sqlite3.connect(db)
    try:
        schema = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        rows = conn.execute("SELECT * FROM operations ORDER BY run_id, op_seq").fetchall()
    finally:
        conn.close()
    return schema, rows


def _open_ledger_in_thread(db: Path) -> tuple[list[BaseException], float, bool]:
    """Open ``OperationLedger(db)`` on a daemon thread.

    Returns its errors, the elapsed seconds, and whether it finished within the bound. A
    thread, so that a regression that waits forever fails the bound instead of hanging the
    test run. The thread is a daemon so a stuck one cannot block interpreter exit.
    """

    errors: list[BaseException] = []

    def opener() -> None:
        try:
            OperationLedger(db, schema_lock=True).close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    worker = threading.Thread(target=opener, daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(timeout=_LOCK_WAIT_S + _JOIN_MARGIN_S)
    elapsed = time.monotonic() - started
    return errors, elapsed, not worker.is_alive()


def test_ops_rebuild_refused_while_root_lock_held_then_succeeds_after_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bounded wait, then typed RootBusy with nothing written; open succeeds once released."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", str(_LOCK_WAIT_S))
    db = tmp_path / "operations.db"
    _make_legacy_operations_db(db)
    before = _db_snapshot(db)

    lock = RootLock(tmp_path / ".lock")
    lock.try_exclusive()
    errors, elapsed, finished = _open_ledger_in_thread(db)
    lock.release()
    assert finished, "operations schema open did not finish within the bound"
    assert len(errors) == 1
    assert isinstance(errors[0], RootBusy)
    assert elapsed >= _LOCK_WAIT_S / 2
    assert _db_snapshot(db) == before

    errors, _, finished = _open_ledger_in_thread(db)
    assert finished
    assert errors == []
    ops = OperationLedger(db)
    try:
        assert ops.get("old", 1, "n", 0) == (True, {"v": 1})
    finally:
        ops.close()


def test_init_operations_table_under_own_shared_lock_raises_root_busy_within_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shared lock this process already holds on the same ``.lock`` file must not
    deadlock the schema rebuild: it is refused with RootBusy within the bound, nothing
    written."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", str(_LOCK_WAIT_S))
    db = tmp_path / "operations.db"
    _make_legacy_operations_db(db)
    before = _db_snapshot(db)

    own_shared = RootLock(tmp_path / ".lock")
    own_shared.acquire_shared()
    errors, elapsed, finished = _open_ledger_in_thread(db)
    own_shared.release()
    assert finished, "operations schema open did not finish within the bound"
    assert len(errors) == 1
    assert isinstance(errors[0], RootBusy)
    assert elapsed >= _LOCK_WAIT_S / 2
    assert _db_snapshot(db) == before
