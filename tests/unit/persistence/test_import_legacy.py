"""REL-3: import checkpoints.db / operations.db into runtime.db."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.criteria import TaskCriterion
from contracts.run import RunManifest, RunState, Task
from recertia.graph.ops import OperationLedger
from recertia.graph.persistence import open_run_persistence
from recertia.graph.store import CheckpointStore
from recertia.ops.backup import BackupError, _exclusive_root_locks, backup_tree
from recertia.persistence.db import MigrationChecksumError
from recertia.persistence.import_legacy import (
    IMPORT_ID,
    import_legacy_runtime,
    legacy_import_complete,
    legacy_sqlite_pending,
)
from recertia.persistence.layout import RuntimeLayout
from recertia.persistence.root_lock import RootBusy, RootLock, RootLockBusy


def _state(run_id: str, terminal: str | None = None) -> RunState:
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
        terminal=terminal,
    )


def test_import_copies_rows_drops_pending_and_renames(tmp_path: Path) -> None:
    layout = RuntimeLayout(tmp_path).local_runs()
    store = CheckpointStore(layout.checkpoints_db)
    try:
        store.save("run-a", 0, "intake", "retrieve", _state("run-a", terminal="solved"))
    finally:
        store.close()
    ops = OperationLedger(layout.operations_db)
    try:
        ops.put("run-a", 1, "solve", 0, {"ok": True})
        ops._conn.execute(
            "INSERT INTO operations (run_id, incarnation, attempt_no, node, op_seq, result_json) "
            "VALUES (?, 1, 1, 'solve', 1, ?)",
            ("run-a", json.dumps({"__pending__": True})),
        )
        ops._conn.commit()
    finally:
        ops.close()

    assert import_legacy_runtime(layout) is True
    assert layout.checkpoints_db.is_file()
    assert layout.operations_db.is_file()
    assert (tmp_path / "checkpoints.db.migrated").is_file()
    assert (tmp_path / "operations.db.migrated").is_file()

    conn = sqlite3.connect(layout.runtime_db)
    try:
        ckpt = conn.execute(
            "SELECT run_id, seq, node FROM checkpoints"
        ).fetchall()
        assert ckpt == [("run-a", 0, "intake")]
        op_rows = conn.execute(
            "SELECT op_seq, result_json FROM operations ORDER BY op_seq"
        ).fetchall()
        assert len(op_rows) == 1
        assert op_rows[0][0] == 0
        assert json.loads(op_rows[0][1]) == {"ok": True}
        recorded = conn.execute(
            "SELECT version FROM schema_migrations WHERE version=?",
            (IMPORT_ID,),
        ).fetchone()
        assert recorded is not None
        run_row = conn.execute("SELECT status FROM runs WHERE run_id=?", ("run-a",)).fetchone()
        assert run_row == ("solved",)
    finally:
        conn.close()

    assert import_legacy_runtime(layout) is True


def test_unequal_checkpoint_keeps_dest_and_imports_other_seqs(tmp_path: Path) -> None:
    layout = RuntimeLayout(tmp_path).local_runs()
    dest = CheckpointStore(layout.runtime_db)
    src = CheckpointStore(layout.checkpoints_db)
    try:
        dest.save("run-a", 0, "intake", "retrieve", _state("run-a"))
        src.save("run-a", 0, "intake", "plan", _state("run-a"))
        src.save("run-a", 1, "plan", None, _state("run-a"))
    finally:
        dest.close()
        src.close()

    assert import_legacy_runtime(layout) is True
    store = CheckpointStore(layout.runtime_db)
    try:
        assert store.latest_seq("run-a") == 1
        seq0 = store.history("run-a")[0]
        assert seq0[1] == "intake"
        assert seq0[2] == "retrieve"
    finally:
        store.close()
    assert layout.checkpoints_db.is_file()
    assert (tmp_path / "checkpoints.db.migrated").is_file()


def test_equal_checkpoint_is_kept_once(tmp_path: Path) -> None:
    layout = RuntimeLayout(tmp_path).local_runs()
    state = _state("run-a")
    dest = CheckpointStore(layout.runtime_db)
    src = CheckpointStore(layout.checkpoints_db)
    try:
        dest.save("run-a", 0, "intake", "retrieve", state)
        src.save("run-a", 0, "intake", "retrieve", state)
    finally:
        dest.close()
        src.close()

    assert import_legacy_runtime(layout) is True
    conn = sqlite3.connect(layout.runtime_db)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()
        assert rows == (1,)
    finally:
        conn.close()
    assert layout.checkpoints_db.is_file()
    assert (tmp_path / "checkpoints.db.migrated").is_file()


def test_root_lock_busy_fails_import(tmp_path: Path) -> None:
    layout = RuntimeLayout(tmp_path).local_runs()
    store = CheckpointStore(layout.checkpoints_db)
    try:
        store.save("run-a", 0, "intake", None, _state("run-a"))
    finally:
        store.close()
    held = RootLock(layout.parent.root_lock)
    held.try_exclusive()
    try:
        with pytest.raises(RootBusy, match="cannot import leftover runtime files"):
            import_legacy_runtime(layout, timeout_s=0)
        assert layout.checkpoints_db.is_file()
        assert not (tmp_path / "runtime.db").exists()
    finally:
        held.release()


def _open_in_other_thread(root: Path) -> BaseException | None:
    box: list[BaseException] = []

    def run() -> None:
        try:
            opened = open_run_persistence(root)
            opened.close()
        except BaseException as exc:  # noqa: BLE001
            box.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(timeout=8)
    return box[0] if box else None


def test_lock_busy_open_raises_without_writing_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A *different* thread must not write a ghost runtime.db while exclusive is held."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = tmp_path / "runs"
    p0 = open_run_persistence(root, migrate="off")
    try:
        p0.checkpoints.save("old-run", 1, "n", None, _state("old-run"))
    finally:
        p0.close()
    lock = RootLock(root / ".lock")
    lock.try_exclusive()
    try:
        err = _open_in_other_thread(root)
        assert isinstance(err, RootLockBusy)
        assert "cannot import leftover runtime files" in str(err)
        assert not (root / "runtime.db").exists()
    finally:
        lock.release()
    imported = open_run_persistence(root)
    try:
        assert imported.checkpoints.list_run_ids() == ["old-run"]
        assert imported.checkpoints.latest_seq("old-run") == 1
    finally:
        imported.close()


def test_exclusive_held_by_this_thread_is_removed() -> None:
    """REC-056b1: the pid/tid holder helper was a stub that always said False. It is gone.

    The lock is the flock alone, so nothing may ask the lock file who holds it.
    """

    with pytest.raises(ImportError, match="exclusive_held_by_this_thread"):
        from recertia.persistence.root_lock import exclusive_held_by_this_thread  # type: ignore[attr-defined]

        del exclusive_held_by_this_thread


def test_same_thread_holder_gets_root_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No same-thread exception: opening while this thread holds exclusive is RootBusy."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = tmp_path / "runs"
    p0 = open_run_persistence(root, migrate="off")
    try:
        p0.checkpoints.save("old-run", 1, "n", None, _state("old-run"))
    finally:
        p0.close()
    lock = RootLock(root / ".lock")
    lock.try_exclusive()
    try:
        with pytest.raises(RootBusy, match="cannot import leftover runtime files"):
            open_run_persistence(root)
        assert not (root / "runtime.db").exists()
    finally:
        lock.release()
    imported = open_run_persistence(root)
    try:
        assert imported.checkpoints.list_run_ids() == ["old-run"]
    finally:
        imported.close()


def test_sigkill_holder_leaves_empty_lock_file(tmp_path: Path) -> None:
    import subprocess
    import sys

    lock_path = tmp_path / ".lock"
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, sys\n"
            "from pathlib import Path\n"
            "from recertia.persistence.root_lock import RootLock\n"
            f"l = RootLock(Path({str(lock_path)!r}))\n"
            "l.try_exclusive()\n"
            "os.kill(os.getpid(), 9)\n",
        ],
        check=False,
    )
    assert lock_path.is_file()
    assert lock_path.read_text() == ""


def test_migrate_off_after_import_sees_post_import_writes(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    legacy = open_run_persistence(root, migrate="off")
    try:
        for seq in (1, 2, 3):
            legacy.checkpoints.save("legacy-run", seq, "n", None, _state("legacy-run"))
    finally:
        legacy.close()
    imported = open_run_persistence(root)
    try:
        imported.checkpoints.save("legacy-run", 4, "post-import", None, _state("legacy-run"))
    finally:
        imported.close()
    off = open_run_persistence(root, migrate="off")
    try:
        assert off.checkpoints.latest_seq("legacy-run") == 4
        assert off.checkpoints._db_path == root / "runtime.db"
    finally:
        off.close()


def test_migrate_off_after_import_still_sees_state(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    legacy = open_run_persistence(root, migrate="off")
    try:
        for i in range(5):
            legacy.checkpoints.save("legacy", i + 1, "n", None, _state("legacy"))
        legacy.operations.put("legacy", 1, "n", 0, {"v": 1})
    finally:
        legacy.close()
    open_run_persistence(root).close()
    off = open_run_persistence(root, migrate="off")
    try:
        assert off.checkpoints.latest_seq("legacy") == 5
    finally:
        off.close()


def test_import_ops_rebuild_resumable_after_rel3_leftover(tmp_path: Path) -> None:
    layout = RuntimeLayout(tmp_path).local_runs()
    from recertia.persistence import import_legacy as il

    conn = sqlite3.connect(layout.runtime_db)
    conn.execute(
        "CREATE TABLE operations ("
        "run_id TEXT NOT NULL, attempt_no INTEGER NOT NULL, node TEXT NOT NULL, "
        "op_seq INTEGER NOT NULL, result_json TEXT NOT NULL, "
        "PRIMARY KEY (run_id, attempt_no, node, op_seq))"
    )
    conn.execute("INSERT INTO operations VALUES ('old', 1, 'n', 0, '{\"v\":1}')")
    conn.execute(il._LIVE_OPERATIONS_DDL.replace("operations", "operations_rel3", 1))
    conn.commit()
    conn.close()
    store = CheckpointStore(layout.checkpoints_db)
    try:
        store.save("old", 1, "n", None, _state("old"))
    finally:
        store.close()
    assert import_legacy_runtime(layout) is True
    ops = OperationLedger(layout.runtime_db)
    try:
        applied, result = ops.get("old", 1, "n", 0)
        assert applied is True
        assert result == {"v": 1}
    finally:
        ops.close()


def test_open_run_persistence_auto_uses_runtime_db(tmp_path: Path) -> None:
    persistence = open_run_persistence(tmp_path / "runs")
    try:
        persistence.checkpoints.save("run-a", 0, "intake", None, _state("run-a"))
        assert persistence.checkpoints._db_path == tmp_path / "runs" / "runtime.db"
        assert persistence.operations._db_path == tmp_path / "runs" / "runtime.db"
    finally:
        persistence.close()
    assert (tmp_path / "runs" / "runtime.db").is_file()
    assert not (tmp_path / "runs" / "checkpoints.db").exists()


def test_open_run_persistence_off_keeps_legacy_files(tmp_path: Path) -> None:
    persistence = open_run_persistence(tmp_path / "runs", migrate="off")
    try:
        persistence.checkpoints.save("run-a", 0, "intake", None, _state("run-a"))
        assert persistence.checkpoints._db_path == tmp_path / "runs" / "checkpoints.db"
        assert persistence.operations._db_path == tmp_path / "runs" / "operations.db"
    finally:
        persistence.close()
    assert (tmp_path / "runs" / "checkpoints.db").is_file()
    assert not (tmp_path / "runs" / "runtime.db").exists()


def test_open_run_persistence_manual_refuses_legacy(tmp_path: Path) -> None:
    layout = RuntimeLayout(tmp_path / "runs").local_runs()
    store = CheckpointStore(layout.checkpoints_db)
    try:
        store.save("run-a", 0, "intake", None, _state("run-a"))
    finally:
        store.close()
    with pytest.raises(MigrationChecksumError, match="migrate apply"):
        open_run_persistence(tmp_path / "runs", migrate="manual")
    assert layout.checkpoints_db.is_file()


def test_open_run_persistence_auto_imports_then_reads(tmp_path: Path) -> None:
    layout = RuntimeLayout(tmp_path / "runs").local_runs()
    store = CheckpointStore(layout.checkpoints_db)
    try:
        store.save("run-a", 0, "intake", "retrieve", _state("run-a"))
    finally:
        store.close()
    persistence = open_run_persistence(tmp_path / "runs", migrate="auto")
    try:
        latest = persistence.checkpoints.latest("run-a")
        assert latest is not None
        assert latest[1] == "intake"
        assert layout.checkpoints_db.is_file()
    finally:
        persistence.close()


def test_lock_busy_open_then_import_keeps_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Other-thread busy open fails closed; after unlock, import still sees leftover seqs."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = tmp_path / "runs"
    p0 = open_run_persistence(root, migrate="off")
    try:
        for seq in (1, 2, 3):
            p0.checkpoints.save("legacy-run", seq, f"n{seq}", None, _state("legacy-run"))
    finally:
        p0.close()
    lock = RootLock(root / ".lock")
    lock.try_exclusive()
    try:
        err = _open_in_other_thread(root)
        assert isinstance(err, RootBusy)
        assert "cannot import leftover runtime files" in str(err)
        assert not (root / "runtime.db").exists()
    finally:
        lock.release()
    imported = open_run_persistence(root)
    try:
        assert imported.checkpoints.latest_seq("legacy-run") == 3
    finally:
        imported.close()


def test_legacy_writer_holds_root_lock_against_import(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    writer = open_run_persistence(root, migrate="off")
    try:
        writer.checkpoints.save("legacy", 1, "n", None, _state("legacy"))
        with pytest.raises(RootLockBusy):
            import_legacy_runtime(RuntimeLayout(root).local_runs(), timeout_s=0)
    finally:
        writer.close()
    assert import_legacy_runtime(RuntimeLayout(root).local_runs()) is True


def test_legacy_not_pending_after_completed_import(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    layout = RuntimeLayout(root).local_runs()
    p0 = open_run_persistence(root, migrate="off")
    try:
        p0.checkpoints.save("legacy", 1, "n", None, _state("legacy"))
    finally:
        p0.close()
    open_run_persistence(root).close()
    assert layout.checkpoints_db.is_file()
    assert legacy_sqlite_pending(layout) is False


def _db_byte_snapshot(root: Path) -> dict[str, str]:
    snap: dict[str, str] = {}
    if not root.exists():
        return snap
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        name = path.name
        if not (
            name.endswith(".db")
            or name.endswith(".sqlite")
            or name.endswith("-wal")
            or name.endswith("-shm")
            or ".db." in name
        ):
            continue
        snap[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snap


def _legacy_row_snapshot(root: Path) -> tuple[tuple[object, ...], tuple[object, ...]]:
    ck_rows: list[object] = []
    op_rows: list[object] = []
    ck = root / "checkpoints.db"
    op = root / "operations.db"
    if ck.is_file():
        conn = sqlite3.connect(ck)
        try:
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkpoints'"
            ).fetchone():
                ck_rows = conn.execute(
                    "SELECT run_id, seq, node FROM checkpoints ORDER BY run_id, seq"
                ).fetchall()
        finally:
            conn.close()
    if op.is_file():
        conn = sqlite3.connect(op)
        try:
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'"
            ).fetchone():
                op_rows = conn.execute(
                    "SELECT run_id, attempt_no, node, op_seq, result_json "
                    "FROM operations ORDER BY run_id, op_seq"
                ).fetchall()
        finally:
            conn.close()
    return tuple(ck_rows), tuple(op_rows)


def test_busy_open_raises_root_busy_without_mutating_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O/M3/M4 ruling: typed RootBusy, no writes during the busy window."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = tmp_path / "runs"
    effects: list[str] = []
    p0 = open_run_persistence(root, migrate="off")
    try:
        for seq in (1, 2, 3):
            p0.checkpoints.save("legacy-run", seq, f"n{seq}", None, _state("legacy-run"))
        p0.operations.run_once(
            "legacy-run", 1, "solve", 0, lambda: effects.append("legacy") or {"v": 1}
        )
    finally:
        p0.close()
    before_bytes = _db_byte_snapshot(root)
    before_rows = _legacy_row_snapshot(root)
    lock = RootLock(root / ".lock")
    lock.try_exclusive()
    started = time.monotonic()
    try:
        err = _open_in_other_thread(root)
        assert isinstance(err, RootBusy)
        assert "cannot import leftover runtime files" in str(err)
        assert time.monotonic() - started < 1.5
        assert _db_byte_snapshot(root) == before_bytes
        assert _legacy_row_snapshot(root) == before_rows
        assert not (root / "runtime.db").exists()
        assert effects == ["legacy"]
    finally:
        lock.release()


def test_busy_open_does_not_rerun_legacy_op_after_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = tmp_path / "runs"
    effects: list[str] = []
    p0 = open_run_persistence(root, migrate="off")
    try:
        p0.checkpoints.save("legacy-run", 1, "n", None, _state("legacy-run"))
        p0.operations.run_once(
            "legacy-run", 1, "solve", 0, lambda: effects.append("legacy") or {"v": 1}
        )
    finally:
        p0.close()
    lock = RootLock(root / ".lock")
    lock.try_exclusive()
    try:
        err = _open_in_other_thread(root)
        assert isinstance(err, RootBusy)
        assert effects == ["legacy"]
    finally:
        lock.release()
    opened = open_run_persistence(root)
    try:
        opened.operations.run_once(
            "legacy-run", 1, "solve", 0, lambda: effects.append("busy-rerun") or {"v": 2}
        )
        assert effects == ["legacy"]
        assert opened.checkpoints.latest_seq("legacy-run") == 1
    finally:
        opened.close()


def test_after_busy_release_import_sees_holder_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = tmp_path / "runs"
    p0 = open_run_persistence(root, migrate="off")
    try:
        for seq in (1, 2, 3):
            p0.checkpoints.save("legacy-run", seq, f"n{seq}", None, _state("legacy-run"))
    finally:
        p0.close()
    before_bytes = _db_byte_snapshot(root)
    lock = RootLock(root / ".lock")
    lock.try_exclusive()
    try:
        err = _open_in_other_thread(root)
        assert isinstance(err, RootBusy)
        assert _db_byte_snapshot(root) == before_bytes
        holder = CheckpointStore(root / "checkpoints.db")
        try:
            holder.save("legacy-run", 4, "holder", None, _state("legacy-run"))
        finally:
            holder.close()
    finally:
        lock.release()
    opened = open_run_persistence(root)
    try:
        assert opened.checkpoints.latest_seq("legacy-run") == 4
        nodes = [node for _seq, node, *_rest in opened.checkpoints.history("legacy-run")]
        assert nodes == ["n1", "n2", "n3", "holder"]
    finally:
        opened.close()


def test_busy_open_waits_then_succeeds_after_holder_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "2")
    root = tmp_path / "runs"
    p0 = open_run_persistence(root, migrate="off")
    try:
        for seq in (1, 2, 3):
            p0.checkpoints.save("legacy-run", seq, f"n{seq}", None, _state("legacy-run"))
    finally:
        p0.close()
    before_bytes = _db_byte_snapshot(root)
    lock = RootLock(root / ".lock")
    lock.try_exclusive()
    errors: list[BaseException] = []
    seen: dict[str, int | None] = {}

    def opener() -> None:
        try:
            opened = open_run_persistence(root)
            seen["seq"] = opened.checkpoints.latest_seq("legacy-run")
            opened.close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    worker = threading.Thread(target=opener)
    worker.start()
    time.sleep(0.25)
    assert worker.is_alive()
    assert _db_byte_snapshot(root) == before_bytes
    holder = CheckpointStore(root / "checkpoints.db")
    try:
        holder.save("legacy-run", 4, "holder", None, _state("legacy-run"))
    finally:
        holder.close()
    lock.release()
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert errors == []
    assert seen.get("seq") == 4


def test_api_presence_lock_does_not_block_legacy_import(tmp_path: Path) -> None:
    """N: create_app must not hold root/.lock for the process lifetime."""

    from recertia.api import create_app

    root = tmp_path / "api-root"
    layout = RuntimeLayout(root).tenant("t1")
    p0 = open_run_persistence(layout, migrate="off")
    try:
        p0.checkpoints.save("legacy-t1", 1, "n", None, _state("legacy-t1"))
    finally:
        p0.close()
    app = create_app(root=root)
    assert Path(app.state.root_lock.path) == root / "api.lock"
    opened = open_run_persistence(layout)
    try:
        assert opened.checkpoints.latest_seq("legacy-t1") == 1
    finally:
        opened.close()


# --- REC-058 (#144): writers on an already-imported root honour backup/restore's .lock ---

_BUSY_BOUND_S = 5.0  # RECERTIA_IMPORT_LOCK_TIMEOUT_S (0.25s here) plus slack


def _imported_root(tmp_path: Path) -> Path:
    """A root whose leftover import has already completed (import_legacy returns early)."""

    root = tmp_path / "runs"
    p0 = open_run_persistence(root, migrate="off")
    try:
        p0.checkpoints.save("old-run", 1, "n", None, _state("old-run"))
    finally:
        p0.close()
    open_run_persistence(root).close()
    layout = RuntimeLayout(root).local_runs()
    assert legacy_import_complete(layout.runtime_db)
    assert legacy_sqlite_pending(layout) is False
    return root


def _start_writer(root: Path, run_id: str) -> tuple[threading.Thread, dict[str, object]]:
    """Open the root and write one checkpoint on another thread."""

    box: dict[str, object] = {"wrote": False}

    def run() -> None:
        started = time.monotonic()
        try:
            opened = open_run_persistence(root)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc
            box["elapsed"] = time.monotonic() - started
            return
        try:
            opened.checkpoints.save(run_id, 1, "n", None, _state(run_id))
            box["wrote"] = True
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc
        finally:
            opened.close()
            box["elapsed"] = time.monotonic() - started

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return worker, box


def test_write_on_imported_root_while_backup_lock_held_is_blocked_or_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = _imported_root(tmp_path)
    before = _db_byte_snapshot(root)
    api_lock, lock = _exclusive_root_locks(root, action="backup")
    try:
        worker, box = _start_writer(root, "busy-run")
        worker.join(timeout=_BUSY_BOUND_S)
        blocked = worker.is_alive()
        if not blocked:
            assert isinstance(box.get("error"), RootBusy), box
            assert float(box["elapsed"]) <= _BUSY_BOUND_S  # type: ignore[arg-type]
        assert box["wrote"] is False
        assert _db_byte_snapshot(root) == before
    finally:
        lock.release()
        api_lock.release()
    worker.join(timeout=_BUSY_BOUND_S)
    assert not worker.is_alive()


def test_write_on_imported_root_succeeds_after_backup_lock_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = _imported_root(tmp_path)
    before = _db_byte_snapshot(root)
    api_lock, lock = _exclusive_root_locks(root, action="restore")
    try:
        worker, box = _start_writer(root, "busy-run")
        worker.join(timeout=_BUSY_BOUND_S)
        assert box["wrote"] is False
        assert _db_byte_snapshot(root) == before
    finally:
        lock.release()
        api_lock.release()
    worker.join(timeout=_BUSY_BOUND_S)
    assert not worker.is_alive()
    if not box["wrote"]:
        # Refused while busy: the same write, retried after release, lands.
        assert isinstance(box.get("error"), RootBusy), box
        retry, box = _start_writer(root, "busy-run")
        retry.join(timeout=_BUSY_BOUND_S)
        assert not retry.is_alive()
    assert box["wrote"] is True, box
    assert "error" not in box, box
    opened = open_run_persistence(root)
    try:
        assert opened.checkpoints.list_run_ids() == ["busy-run", "old-run"]
        assert opened.checkpoints.latest_seq("busy-run") == 1
    finally:
        opened.close()


def test_open_on_imported_root_under_backup_lock_raises_root_busy_within_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chosen mode: bounded wait, then typed RootBusy (not a hang, not a generic error)."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = _imported_root(tmp_path)
    api_lock, lock = _exclusive_root_locks(root, action="backup")
    try:
        worker, box = _start_writer(root, "busy-run")
        worker.join(timeout=_BUSY_BOUND_S)
        assert not worker.is_alive(), "open hung while the root lock was held"
        assert isinstance(box.get("error"), RootBusy), box
        assert "cannot open the runtime root while the root lock is held" in str(box["error"])
        assert float(box["elapsed"]) <= _BUSY_BOUND_S  # type: ignore[arg-type]
    finally:
        lock.release()
        api_lock.release()


def test_open_handle_on_imported_root_keeps_backup_out_until_close(tmp_path: Path) -> None:
    """The shared lock lives as long as the handle, so backup cannot start under a writer."""

    root = _imported_root(tmp_path)
    archive = tmp_path / "backup.tar.gz"
    opened = open_run_persistence(root)
    try:
        with pytest.raises(BackupError, match="backup refused"):
            backup_tree(root, archive)
        opened.checkpoints.save("live-run", 1, "n", None, _state("live-run"))
    finally:
        opened.close()
    assert backup_tree(root, archive) == archive.resolve()
