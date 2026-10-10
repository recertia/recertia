"""Trajectory tape (runs/trajectories/<run_id>.jsonl) stays valid and duplicate-free across
kill/resume. Every line must parse as contracts.trajectory.TrajectoryEvent (extra='forbid'),
seq must be contiguous from 0, and a resumed run must not re-emit already-taped events.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.criteria import TaskCriterion
from contracts.run import Task
from contracts.trajectory import TrajectoryEvent
from recertia.graph.engine import GraphOrchestrator

SCRIPT = ["python3 -c \"open('output.txt','w').write('done')\""]


def _task() -> Task:
    return Task(task_id="t1", request="w", submitted_at=datetime.now(timezone.utc))


def _tape(runs: Path, run_id: str) -> list[TrajectoryEvent]:
    lines = (runs / "trajectories" / f"{run_id}.jsonl").read_text().splitlines()
    return [TrajectoryEvent.model_validate_json(ln) for ln in lines if ln.strip()]


def _key(e: TrajectoryEvent) -> tuple:
    return (e.node, e.attempt_no, e.event_kind, e.criterion_id, e.skill_id)


@pytest.mark.parametrize("kill_after", [1, 3, 4, 5])
def test_tape_after_resume_matches_uninterrupted(
    tmp_path: Path, proven_criterion: TaskCriterion, kill_after: int
) -> None:
    base_runs = tmp_path / "base"
    (tmp_path / "w0").mkdir()
    o = GraphOrchestrator(base_runs)
    o.start("r", _task(), [proven_criterion], workdir=tmp_path / "w0", script=SCRIPT)
    o.close()
    base = _tape(base_runs, "r")

    runs = tmp_path / "runs"
    wd = tmp_path / "w1"
    wd.mkdir()
    o1 = GraphOrchestrator(runs)
    o1.start(
        "r",
        _task(),
        [proven_criterion],
        workdir=wd,
        script=SCRIPT,
        max_steps=kill_after,
    )
    o1.close()
    o2 = GraphOrchestrator(runs)
    try:
        assert o2.resume("r", workdir=wd, script=SCRIPT).terminal == "solved"
    finally:
        o2.close()
    tape = _tape(runs, "r")

    assert [e.seq for e in tape] == list(range(len(tape))), "seq gap/duplicate on tape"
    assert {e.run_id for e in tape} == {"r"}
    assert [_key(e) for e in tape] == [_key(e) for e in base], "resume re-emitted or dropped events"
