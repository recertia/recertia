"""Eval observations are queued with the finalize checkpoint and delivered once."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from contracts.criteria import TaskCriterion
from contracts.run import RunManifest, RunState, Task
from recertia.evals.outbox import drain_eval_outbox
from recertia.evals.store import EvalStore
from recertia.graph.store import CheckpointConflict, CheckpointStore, EvalOutboxWrite


def _state(run_id: str, *, attempt_no: int = 1) -> RunState:
    return RunState(
        run_id=run_id,
        task=Task(
            task_id=run_id,
            request="x",
            task_class="repo-chore",
            submitted_at=datetime.now(timezone.utc),
        ),
        manifest=RunManifest(index_snapshot_id="snap", criteria_hash="h"),
        criteria=[
            TaskCriterion(id="c", kind="command", run="true", source="caller"),
        ],
        criteria_locked_at=datetime.now(timezone.utc),
        terminal="solved",
        attempt_no=attempt_no,
    )


def _outbox(state: RunState) -> EvalOutboxWrite:
    return EvalOutboxWrite(
        observation_key=f"local/{state.run_id}/{state.attempt_no}",
        tenant_id="local",
        run_id=state.run_id,
        attempt_no=state.attempt_no,
        payload_json=state.model_dump_json(),
    )


def test_failed_checkpoint_does_not_leave_an_outbox_row(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "checkpoints.db")
    state = _state("run-1")
    store.save("run-1", 0, "finalize", None, state, eval_outbox=_outbox(state))
    try:
        store.save("run-1", 0, "finalize", None, state, eval_outbox=_outbox(_state("other")))
        raise AssertionError("expected CheckpointConflict")
    except CheckpointConflict:
        pass
    pending = store.pending_eval_outbox()
    assert [row.observation_key for row in pending] == ["local/run-1/1"]
    store.close()


def test_drain_is_idempotent(tmp_path: Path) -> None:
    checkpoints = CheckpointStore(tmp_path / "checkpoints.db")
    state = _state("run-1")
    checkpoints.save("run-1", 0, "finalize", None, state, eval_outbox=_outbox(state))
    evals = EvalStore(tmp_path / "evals.db")
    assert drain_eval_outbox(checkpoints, evals) == 1
    assert drain_eval_outbox(checkpoints, evals) == 0
    rows = evals._conn.execute("SELECT run_id FROM observations").fetchall()
    assert [row[0] for row in rows] == ["run-1"]
    assert checkpoints.pending_eval_outbox() == []
    evals.close()
    checkpoints.close()
