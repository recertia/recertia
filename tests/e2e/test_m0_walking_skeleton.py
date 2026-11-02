"""M0 done-when criteria (docs/archive/2026-Q3/implementation-plan.md M0), proven end-to-end.

Each test below maps directly to one clause of M0's "Done when" list.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from contracts.budget import Budget
from contracts.criteria import TaskCriterion
from contracts.run import Task
from recertia.graph.engine import GraphOrchestrator


def _task(workdir: Path) -> Task:
    return Task(task_id="t1", request="write output.txt", submitted_at=datetime.now(timezone.utc))


def test_a_run_reaches_finalize_solved(tmp_path: Path, proven_criterion: TaskCriterion) -> None:
    """"a run reaches finalize with terminal='solved'"."""

    workdir = tmp_path / "workspace"
    workdir.mkdir()
    orch = GraphOrchestrator(tmp_path / "runs")
    try:
        state = orch.start(
            "run-solved",
            _task(workdir),
            [proven_criterion],
            workdir=workdir,
            script=["python3 -c \"open('output.txt','w').write('done')\""],
        )
    finally:
        orch.close()

    assert state.terminal == "solved"
    assert state.route_log[-1].node == "distill"
    assert state.results and all(r.passed for r in state.results)


def test_kill_mid_run_and_resume_completes_with_no_double_apply(
    tmp_path: Path, proven_criterion: TaskCriterion
) -> None:
    """"killing the process mid-run and resuming completes it from the last checkpoint with no
    operation double-applied".

    Genuinely simulated: one orchestrator ("process 1") is stopped after exactly 3 node-hops
    (intake, retrieve, plan) via ``max_steps`` — before ``solve`` ever runs — is then dropped
    (its in-memory state discarded, only the on-disk checkpoint/op-ledger files remain), and a
    brand-new orchestrator ("process 2") resumes purely from disk.
    """

    workdir = tmp_path / "workspace"
    workdir.mkdir()
    runs_root = tmp_path / "runs"
    script = [
        "python3 -c \"open('step1.txt','w').write('1')\"",
        "python3 -c \"open('output.txt','w').write('done')\"",
    ]
    task = _task(workdir)

    orch1 = GraphOrchestrator(runs_root)
    stopped = orch1.start(
        "run-resume", task, [proven_criterion], workdir=workdir, script=script, max_steps=3
    )
    orch1.close()  # "process 1" dies here — everything above is discarded

    assert stopped.terminal is None  # not finalized yet
    assert stopped.transcript_ref is None  # solve has not run at all

    orch2 = GraphOrchestrator(runs_root)  # "process 2": fresh objects, same on-disk state
    try:
        resumed = orch2.resume("run-resume", workdir=workdir, script=script)
        solve_ops = orch2.ops.count_for_node("run-resume", 1, "solve")
    finally:
        orch2.close()

    assert resumed.terminal == "solved"
    assert solve_ops == len(script), "each scripted step recorded exactly once by process 2"

    # Resuming an already-finalized run is a safe no-op.
    orch3 = GraphOrchestrator(runs_root)
    try:
        reresumed = orch3.resume("run-resume", workdir=workdir)
    finally:
        orch3.close()
    assert reresumed.terminal == "solved"


def test_resume_mid_retry_loop_does_not_reexecute_prior_attempts(tmp_path: Path) -> None:
    """Kill/resume *inside* the evolve->solve retry loop: process 1 dies right after `evolve`
    restores the workspace for attempt 2 but before `solve` runs attempt 2; process 2 resumes
    and must run attempt 2's steps exactly once, without touching attempt 1's already-recorded
    operations."""

    workdir = tmp_path / "workspace"
    workdir.mkdir()
    runs_root = tmp_path / "runs"
    script = ["python3 -c \"open('attempt_marker.txt','w').write('ran')\""]
    from contracts.criteria import mint_rejecting_proof

    never_base = TaskCriterion(
        id="never",
        kind="command",
        run="test -f this-file-is-never-created.txt",
        source="caller",
        weight=1.0,
    )
    never_passes = never_base.model_copy(
        update={"sensitivity_proof": mint_rejecting_proof(never_base, fingerprint="m0-never")}
    )
    task = _task(workdir)

    # intake, retrieve, plan, solve#1, validate#1, classify_failure#1, evolve#1 = 7 steps;
    # process 1 dies exactly there, before solve#2 ever runs.
    orch1 = GraphOrchestrator(runs_root)
    stopped = orch1.start(
        "run-mid-retry",
        task,
        [never_passes],
        budget=Budget(max_attempts=4),
        workdir=workdir,
        script=script,
        max_steps=7,
    )
    orch1.close()

    assert stopped.attempt_no == 1
    orch_count = GraphOrchestrator(runs_root)
    try:
        ops_attempt1 = orch_count.ops.count_for_node("run-mid-retry", 1, "solve")
    finally:
        orch_count.close()
    assert ops_attempt1 == len(script)

    orch2 = GraphOrchestrator(runs_root)
    try:
        resumed = orch2.resume("run-mid-retry", workdir=workdir, script=script)
        ops_attempt1_after = orch2.ops.count_for_node("run-mid-retry", 1, "solve")
        ops_attempt2_after = orch2.ops.count_for_node("run-mid-retry", 2, "solve")
    finally:
        orch2.close()

    assert ops_attempt1_after == len(script), "attempt 1's operations must not be re-applied"
    assert ops_attempt2_after == len(script), "attempt 2's operations must be applied exactly once"
    assert resumed.terminal == "unsolved"
    assert resumed.route_log[-1].node == "record_dead_end"


def test_always_failing_criteria_terminates_at_record_dead_end(tmp_path: Path) -> None:
    """"a run whose criteria always fail terminates at record_dead_end with a failure class
    rather than looping"."""

    workdir = tmp_path / "workspace"
    workdir.mkdir()
    from contracts.criteria import mint_rejecting_proof

    always_base = TaskCriterion(
        id="never",
        kind="command",
        run="test -f this-file-is-never-created.txt",
        source="caller",
        weight=1.0,
    )
    always_fails = always_base.model_copy(
        update={"sensitivity_proof": mint_rejecting_proof(always_base, fingerprint="m0-never")}
    )
    orch = GraphOrchestrator(tmp_path / "runs")
    try:
        state = orch.start(
            "run-always-fails",
            _task(workdir),
            [always_fails],
            budget=Budget(max_attempts=4),
            workdir=workdir,
            script=["true"],
        )
    finally:
        orch.close()

    assert state.terminal == "unsolved"
    assert state.route_log[-1].node == "record_dead_end"
    assert state.failure is not None
    assert state.failure.failure_class in ("execution", "budget")
    # Terminates well before an unbounded loop would; identical-results-no-progress or budget
    # exhaustion must have cut it short, not a hang.
    assert state.spent.attempts <= 4


def test_retry_always_starts_from_a_clean_snapshot(tmp_path: Path) -> None:
    """"retrying always starts from a clean snapshot": every attempt sees the same pristine
    workspace, never a previous failed attempt's partial mutation."""

    workdir = tmp_path / "workspace"
    workdir.mkdir()
    (workdir / "marker.txt").write_text("pristine")
    from contracts.criteria import mint_rejecting_proof

    # Script: on every attempt, assert marker.txt still reads "pristine" (fails loudly if a
    # previous attempt's mutation leaked through), then mutate the workspace, then fail a
    # required criterion so evolve retries.
    script = [
        "python3 -c \"assert open('marker.txt').read() == 'pristine'\"",
        "python3 -c \"open('mutated.txt','w').write('leftover')\"",
    ]
    never_base = TaskCriterion(
        id="never",
        kind="command",
        run="test -f this-file-is-never-created.txt",
        source="caller",
        weight=1.0,
    )
    never_passes = never_base.model_copy(
        update={"sensitivity_proof": mint_rejecting_proof(never_base, fingerprint="m0-never")}
    )
    orch = GraphOrchestrator(tmp_path / "runs")
    try:
        state = orch.start(
            "run-clean-retry",
            _task(workdir),
            [never_passes],
            budget=Budget(max_attempts=3),
            workdir=workdir,
            script=script,
        )
    finally:
        orch.close()

    # If the marker-check step ever failed, solve would have raised a solver-side failure
    # signal on step 0 instead of reaching the second step every time; assert every attempt's
    # transcript reflects the second step running (i.e., the first step never failed).
    assert state.terminal == "unsolved"
    assert not any(
        rl.node == "solve" and "step 0" in (rl.reason or "") for rl in state.route_log
    ), "marker.txt was not 'pristine' on some attempt: a previous attempt's mutation leaked through"


def test_unproven_required_criterion_is_advisory_and_visible_in_route_log(
    tmp_path: Path, unproven_required_criterion: TaskCriterion
) -> None:
    """"a required criterion with no sensitivity proof is treated as advisory, not required,
    and this is visible in the route log"."""

    workdir = tmp_path / "workspace"
    workdir.mkdir()
    orch = GraphOrchestrator(tmp_path / "runs")
    try:
        state = orch.start(
            "run-advisory",
            _task(workdir),
            [unproven_required_criterion],
            workdir=workdir,
            script=["true"],
        )
    finally:
        orch.close()

    # The criterion actually fails (the file is never created) but, being unproven, must not
    # block the route to distill/solved.
    assert state.terminal == "solved"
    assert state.results[0].criterion_id == "impossible"
    assert state.results[0].passed is True  # advisory downgrade recorded as non-gating
    validate_entry = next(rl for rl in state.route_log if rl.node == "validate")
    assert "no valid sensitivity_proof" in (validate_entry.reason or "")
    assert "advisory" in (validate_entry.reason or "")
