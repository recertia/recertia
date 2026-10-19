"""Checkpoint and trajectory rows commit in one transaction (REL-5a / F16)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.criteria import TaskCriterion
from contracts.run import RunManifest, RunState, Task
from contracts.trajectory import TrajectoryEvent
from recertia.faults import FaultInjected
from recertia.graph.store import CheckpointConflict, CheckpointStore


def _state(run_id: str = "r") -> RunState:
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


def _event(run_id: str = "r", seq: int = 0) -> TrajectoryEvent:
    return TrajectoryEvent(
        run_id=run_id,
        seq=seq,
        node="intake",
        attempt_no=0,
        event_kind="criteria_locked",
        at=datetime.now(timezone.utc),
    )


def test_before_commit_kill_leaves_no_checkpoint_or_tape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_ALLOW_FAULTS", "1")
    monkeypatch.setenv("RECERTIA_FAULTS", "checkpoint.before_commit:kill")
    store = CheckpointStore(tmp_path / "ck.db")
    try:
        with pytest.raises(FaultInjected):
            store.save(
                "r",
                0,
                "intake",
                "retrieve",
                _state(),
                trajectory=[_event()],
            )
        assert store.latest("r") is None
        assert store.list_trajectory_events("r") == []
    finally:
        store.close()


def test_checkpoint_conflict_does_not_duplicate_trajectory(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "ck.db")
    try:
        first = _event(seq=0)
        store.save("r", 0, "intake", "retrieve", _state(), trajectory=[first])
        with pytest.raises(CheckpointConflict):
            store.save("r", 0, "intake", "retrieve", _state(), trajectory=[_event(seq=99)])
        events = store.list_trajectory_events("r")
        assert [e.seq for e in events] == [0]
        assert events[0].event_kind == "criteria_locked"
    finally:
        store.close()


def test_trajectory_seq_is_allocated_inside_the_transaction(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "ck.db")
    try:
        store.save("r", 0, "intake", "retrieve", _state(), trajectory=[_event(), _event()])
        store.save(
            "r",
            1,
            "retrieve",
            "plan",
            _state(),
            trajectory=[_event(seq=0)],
        )
        events = store.list_trajectory_events("r")
        assert [e.seq for e in events] == [0, 1, 2]
        rows = store._conn.execute(
            "SELECT hop_seq FROM trajectory_events WHERE run_id='r' ORDER BY seq"
        ).fetchall()
        assert [int(row[0]) for row in rows] == [0, 0, 1]
    finally:
        store.close()
