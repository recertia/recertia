"""REL-3: import checkpoints.db / operations.db into runtime.db."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.criteria import TaskCriterion
from contracts.run import RunManifest, RunState, Task
from recertia.graph.ops import OperationLedger
from recertia.graph.persistence import open_run_persistence
from recertia.graph.store import CheckpointStore
from recertia.persistence.db import MigrationChecksumError
from recertia.persistence.import_legacy import IMPORT_ID, import_legacy_runtime
from recertia.persistence.layout import RuntimeLayout
from recertia.persistence.root_lock import RootLock
from recertia.runtime.errors import StateCorrupt


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
    assert not layout.checkpoints_db.is_file()
    assert not layout.operations_db.is_file()
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


def test_unequal_checkpoint_aborts_and_leaves_legacy(tmp_path: Path) -> None:
    layout = RuntimeLayout(tmp_path).local_runs()
    dest = CheckpointStore(layout.runtime_db)
    src = CheckpointStore(layout.checkpoints_db)
    try:
        dest.save("run-a", 0, "intake", "retrieve", _state("run-a"))
        src.save("run-a", 0, "intake", "plan", _state("run-a"))
    finally:
        dest.close()
        src.close()

    with pytest.raises(StateCorrupt, match="checkpoint conflict"):
        import_legacy_runtime(layout)
    assert layout.checkpoints_db.is_file()
    assert not (tmp_path / "checkpoints.db.migrated").exists()


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
    assert not layout.checkpoints_db.is_file()


def test_root_lock_busy_skips_import(tmp_path: Path) -> None:
    layout = RuntimeLayout(tmp_path).local_runs()
    store = CheckpointStore(layout.checkpoints_db)
    try:
        store.save("run-a", 0, "intake", None, _state("run-a"))
    finally:
        store.close()
    held = RootLock(layout.parent.root_lock)
    held.try_exclusive()
    try:
        assert import_legacy_runtime(layout) is False
        assert layout.checkpoints_db.is_file()
    finally:
        held.release()


def test_lock_busy_open_writes_runtime_db_not_legacy(tmp_path: Path) -> None:
    """CRITICAL: a busy root lock must not fall back to files the importer will retire."""

    root = tmp_path / "runs"
    p0 = open_run_persistence(root, migrate="off")
    try:
        p0.checkpoints.save("old-run", 1, "n", None, _state("old-run"))
    finally:
        p0.close()
    lock = RootLock(root / ".lock")
    lock.try_exclusive()
    try:
        busy = open_run_persistence(root)
        try:
            busy.checkpoints.save("p2-run", 1, "n", None, _state("p2-run"))
            assert busy.checkpoints._db_path == root / "runtime.db"
        finally:
            busy.close()
    finally:
        lock.release()
    imported = open_run_persistence(root)
    try:
        ids = sorted(imported.checkpoints.list_run_ids())
        assert ids == ["old-run", "p2-run"]
    finally:
        imported.close()


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
        assert off.checkpoints._db_path == root / "runtime.db"
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
        assert not layout.checkpoints_db.is_file()
    finally:
        persistence.close()
