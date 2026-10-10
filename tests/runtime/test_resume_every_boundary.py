"""Crash at EVERY node boundary, resume from disk, and require the same outcome.

Extends tests/e2e/test_m0_walking_skeleton.py (which kills at one fixed point, max_steps=3/7)
into a sweep over all hop counts of a solved run. The sweep bounds come from an uninterrupted
baseline run, so a graph that gains or loses nodes is still swept completely and nothing is
ever skipped.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from contracts.criteria import TaskCriterion
from contracts.run import Task
from recertia.graph.engine import GraphOrchestrator

SCRIPT = [
    "python3 -c \"open('log.txt','a').write('x')\"",  # non-idempotent: double-apply shows up
    "python3 -c \"open('output.txt','w').write('done')\"",
]


def _task() -> Task:
    return Task(task_id="t1", request="write output.txt", submitted_at=datetime.now(timezone.utc))


def _baseline_hops(tmp_path: Path, crit: TaskCriterion) -> int:
    wd = tmp_path / "base-ws"
    wd.mkdir()
    orch = GraphOrchestrator(tmp_path / "base-runs")
    try:
        st = orch.start("run-base", _task(), [crit], workdir=wd, script=SCRIPT)
    finally:
        orch.close()
    assert st.terminal == "solved"
    return len(st.route_log)


def _kill_then_resume(tmp_path: Path, crit: TaskCriterion, kill_after: int, total: int) -> None:
    case = tmp_path / f"kill-{kill_after}"
    wd = case / "ws"
    wd.mkdir(parents=True)
    runs = case / "runs"
    o1 = GraphOrchestrator(runs)
    stopped = o1.start("run-x", _task(), [crit], workdir=wd, script=SCRIPT, max_steps=kill_after)
    o1.close()
    assert stopped.terminal is None, f"kill_after={kill_after}: run finished before the kill"

    o2 = GraphOrchestrator(runs)
    try:
        resumed = o2.resume("run-x", workdir=wd, script=SCRIPT)
        solve_ops = o2.ops.count_for_node("run-x", 1, "solve")
        o2.ledger.verify()
    finally:
        o2.close()

    msg = f"kill_after={kill_after}"
    assert resumed.terminal == "solved", msg
    assert solve_ops == len(SCRIPT), f"{msg}: scripted ops recorded exactly once"
    assert (wd / "log.txt").read_text() == "x", f"{msg}: non-idempotent step applied exactly once"
    assert len(resumed.route_log) == total, msg
    # resuming a finished run again is a no-op
    o3 = GraphOrchestrator(runs)
    try:
        again = o3.resume("run-x", workdir=wd, script=SCRIPT)
    finally:
        o3.close()
    assert again.terminal == "solved" and (wd / "log.txt").read_text() == "x", msg


def test_kill_at_every_boundary_then_resume(tmp_path: Path, proven_criterion: TaskCriterion) -> None:
    total = _baseline_hops(tmp_path, proven_criterion)
    assert total >= 2, f"baseline run has only {total} hop(s); nothing to interrupt"
    # every kill point that leaves the run unfinished: after hop 1 .. after hop total-1
    for kill_after in range(1, total):
        _kill_then_resume(tmp_path, proven_criterion, kill_after, total)
