"""M2 done-when criteria (docs/archive/2026-Q3/implementation-plan.md M2)."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.budget import Budget
from contracts.criteria import SensitivityProof, SkillCertificationCriterion, TaskCriterion
from contracts.resources import ResourceClaim
from contracts.run import Task
from contracts.skill import Hygiene, InputBinding, Provenance, SkillVersion, Step, StepOutput
from contracts.status import Certification
from recertia.governance.sandbox import ApprovalGate
from recertia.graph.engine import GraphOrchestrator
from recertia.memory.affordance import AffordanceStore
from recertia.memory.episodic import CaseRecord, DeadEnd, EpisodicStore
from recertia.memory.procedural.active_set import assign_active_on_approval
from recertia.memory.procedural.store import SkillStore
from recertia.solver.apply import SkillApplicator
from recertia.solver.tools import (
    ClaimScheduler,
    Tool,
    ToolRegistry,
    ToolResult,
    ToolRuntime,
    default_registry,
)
from recertia.solver.transcript import TranscriptStore, TranscriptWriter
from recertia.workspace import WorkspaceManager


def _approved_runtime(registry: ToolRegistry, scheduler: ClaimScheduler | None = None) -> ToolRuntime:
    """Explicit operator grant for tests that exercise write-capable tools."""
    gate = ApprovalGate()
    for tool in registry.names():
        gate.approve(tool, actor="test-operator")
    return ToolRuntime(registry, scheduler, approval_gate=gate)

_NOW = datetime(2026, 7, 31, tzinfo=timezone.utc)


def _criterion(cid: str, command: str) -> TaskCriterion:
    return TaskCriterion(
        id=cid,
        kind="command",
        run=command,
        source="caller",
        weight=1.0,
        sensitivity_proof=SensitivityProof(
            criterion_id=cid,
            negative_fixture="neg",
            rejected=True,
            checked_at=_NOW,
        ),
    )


def _skill(
    skill_id: str,
    steps: list[Step],
    cert_cmd: str = "test -f DONE",
) -> SkillVersion:
    return SkillVersion(
        skill_id=skill_id,
        version=1,
        title=f"Test skill {skill_id} for M2",
        intent=f"A test skill named {skill_id} used only by M2 unit and e2e tests.",
        task_class="repo-chore",
        steps=steps,
        certification_criteria=[
            SkillCertificationCriterion(
                id="done",
                kind="command",
                run=cert_cmd,
                weight=1.0,
                preregistered=True,
                sensitivity_proof=SensitivityProof(
                    criterion_id="done",
                    negative_fixture="neg",
                    rejected=True,
                    checked_at=_NOW,
                ),
            )
        ],
        provenance=Provenance(
            distilled_from_run="m2-test",
            distilled_at=_NOW,
            curation="human_authored",
            derivation="hand_authored",
        ),
        hygiene=Hygiene(secret_scan="passed", scanned_at=_NOW),
    )


def _approve(store: SkillStore, version: SkillVersion) -> None:
    from recertia.memory.procedural.seeds import seed_approved_for_tests

    seed_approved_for_tests(
        store,
        version,
        active=True,
        certification=Certification(
            model_validated_on="m2-test",
            tool_fingerprint={"python": "3.12"},
            recert_status="fresh",
        ),
    )
    # Ensure active assignment matches prior helper behavior.
    status = assign_active_on_approval(store.get_status(version.skill_id, version.version))
    store.write_status(status)


def _m2_stack(tmp_path: Path, store: SkillStore | None = None):
    workspaces = WorkspaceManager(tmp_path / "snaps")
    registry = default_registry()
    tools = _approved_runtime(registry, ClaimScheduler(claim_timeout_s=0.5))
    transcripts = TranscriptStore(tmp_path / "transcripts")
    applicator = SkillApplicator(tools, workspaces, max_parallel_steps=4, claim_timeout_s=0.5)
    episodic = EpisodicStore(tmp_path / "episodic")
    affordances = AffordanceStore(tmp_path / "affordances.json")
    orch = GraphOrchestrator(
        tmp_path / "runs",
        store=store,
        tools=tools,
        transcripts=transcripts,
        applicator=applicator,
        episodic=episodic,
        affordances=affordances,
        env_fingerprint={"python": "3.12"},
    )
    return orch, tools, episodic, affordances, transcripts, workspaces


def test_golden_repo_chore_solved_via_applied_skill(tmp_path: Path) -> None:
    """Golden repo-chore tasks are solved end-to-end via applied skills."""

    store = SkillStore(tmp_path / "skills")
    version = _skill(
        "write-done-marker",
        [
            Step(
                id="write",
                tool="shell",
                intent="Create the DONE marker file.",
                inputs={"command": "echo ok > DONE"},
            )
        ],
    )
    _approve(store, version)

    from recertia.retrieval.index import SkillIndex
    from recertia.retrieval.pipeline import Retriever

    index = SkillIndex(tmp_path / "idx.db")
    index.rebuild(store.iter_loaded())
    retriever = Retriever(index)

    orch, *_ = _m2_stack(tmp_path, store)
    orch.retriever = retriever
    workdir = tmp_path / "work"
    workdir.mkdir()
    try:
        state = orch.start(
            "m2-apply",
            Task(
                task_id="m2-apply",
                request="Create the DONE marker file for write-done-marker",
                task_class="repo-chore",
                submitted_at=_NOW,
            ),
            [_criterion("done", "test -f DONE")],
            workdir=workdir,
            script=None,
        )
    finally:
        orch.close()
        index.close()

    assert state.strategy in ("apply", "adapt")
    assert state.chosen is not None
    assert state.chosen.skill_id == "write-done-marker"
    assert state.terminal == "solved"
    assert state.transcript_ref is not None
    assert state.step_waves, "wave recording required"
    assert (workdir / "DONE").exists()


def test_dead_end_suppresses_repeated_approach(tmp_path: Path) -> None:
    """A task that previously failed a given way does not repeat that approach."""

    episodic = EpisodicStore(tmp_path / "episodic")
    episodic.write(
        CaseRecord(
            case_id="prior-1",
            run_id="prior",
            attempt_no=1,
            task_class="repo-chore",
            outcome="failed",
            failure_class="execution",
            approach="skill:bad-skill@v1",
            dead_end=DeadEnd(
                approach="skill:bad-skill@v1",
                why_failed="steps inapplicable in this environment",
            ),
        )
    )

    from contracts.failure import FailureVerdict
    from contracts.run import RunState, SkillCandidateRef
    from recertia.graph.ops import OperationLedger
    from recertia.ledger import HashChainLedger
    from recertia.nodes.context import NodeContext
    from recertia.nodes.evolve import evolve

    workspaces = WorkspaceManager(tmp_path / "snaps")
    workdir = tmp_path / "work"
    workdir.mkdir()
    ref = workspaces.snapshot(workdir, "r", 0)

    state = RunState(
        run_id="r",
        task=Task(task_id="t", request="x", task_class="repo-chore", submitted_at=_NOW),
        strategy="apply",
        chosen=SkillCandidateRef(skill_id="bad-skill", version=1, score=0.9),
        failure=FailureVerdict(
            failure_class="execution", counts_against_trust=True, escalate_to_human=False
        ),
        workspace_snapshots=[
            __import__("contracts.run", fromlist=["WorkspaceSnapshot"]).WorkspaceSnapshot(
                attempt_no=0, snapshot_ref=ref
            )
        ],
    )
    ctx = NodeContext(
        run_id="r",
        attempt_no=1,
        node="evolve",
        workdir=workdir,
        workspaces=workspaces,
        ledger=HashChainLedger(tmp_path / "ledger.jsonl"),
        ops=OperationLedger(tmp_path / "ops.db"),
        episodic=episodic,
    )
    outcome = evolve(state, ctx)
    assert outcome.state.strategy == "scratch"
    assert "avoid_dead_end" in (outcome.note or "")
    assert outcome.state.chosen is None


def test_flaky_tool_classifies_as_tool_without_trust_impact(tmp_path: Path) -> None:
    """A known-flaky tool produces a tool classification that leaves skill trust untouched."""

    registry = ToolRegistry()

    def flaky_handler(inputs: dict, workdir: Path):
        from recertia.solver.tools import ToolResult

        return ToolResult(
            tool="flaky_net",
            ok=False,
            exit_code=1,
            stderr="FLAKE_SIGNATURE: connection reset",
            error_signature="FLAKE_SIGNATURE",
        )

    registry.register(
        Tool(
            name="flaky_net",
            side_effect="network",
            flaky=True,
            error_signatures=("FLAKE_SIGNATURE",),
        ),
        flaky_handler,
    )
    tools = _approved_runtime(registry)
    affordances = AffordanceStore(tmp_path / "aff.json")
    # Seed affordance history so flake_rate is observable.
    from recertia.solver.tools import ToolResult

    for _ in range(5):
        affordances.record_tool(
            ToolResult(tool="flaky_net", ok=False, exit_code=1, error_signature="FLAKE_SIGNATURE")
        )

    from contracts.failure import FailureSignal
    from contracts.run import RunState
    from recertia.graph.ops import OperationLedger
    from recertia.ledger import HashChainLedger
    from recertia.nodes.classify_failure import classify_failure
    from recertia.nodes.context import NodeContext
    from recertia.workspace import WorkspaceManager

    state = RunState(
        run_id="r",
        task=Task(task_id="t", request="x", submitted_at=_NOW),
        failure_signal=FailureSignal(
            source="solver",
            detail="flaky tool=flaky_net: FLAKE_SIGNATURE: connection reset",
            at=_NOW,
        ),
        spent=__import__("contracts.budget", fromlist=["Spend"]).Spend(attempts=1),
        budget=Budget(max_attempts=4),
    )
    ctx = NodeContext(
        run_id="r",
        attempt_no=1,
        node="classify_failure",
        workdir=tmp_path,
        workspaces=WorkspaceManager(tmp_path / "snaps"),
        ledger=HashChainLedger(tmp_path / "l.jsonl"),
        ops=OperationLedger(tmp_path / "ops.db"),
        tools=tools,
        affordances=affordances,
    )
    outcome = classify_failure(state, ctx)
    assert outcome.state.failure is not None
    assert outcome.state.failure.failure_class == "tool"
    assert outcome.state.failure.counts_against_trust is False


def test_independent_steps_run_concurrently_conflicting_serialise(tmp_path: Path) -> None:
    """Two independent steps run concurrently; same write claim serialises into separate waves."""

    workspaces = WorkspaceManager(tmp_path / "snaps")
    registry = default_registry()
    original = registry.handler("shell")
    # REC-028: measure the behaviour, not wall clock. Each independent step
    # meets the other at a two-party barrier before AND after its real shell
    # call, so both calls sit between two rendezvous: they can only pass if the
    # steps are in flight at the same time (serial execution breaks the barrier
    # after its timeout). The conflicting steps record their intervals, and
    # those must not overlap. The old ``elapsed < 0.09`` check timed the whole
    # apply (snapshots, shell spawns, transcript) and flaked under parallel CPU
    # load. Healthy arrival skew is milliseconds; 5 s bounds a regression.
    barrier_timeout_s = 5
    meet = threading.Barrier(2, timeout=barrier_timeout_s)
    done = threading.Barrier(2, timeout=barrier_timeout_s)
    intervals: dict[str, tuple[float, float]] = {}
    guard = threading.Lock()

    def slow_shell(inputs: dict, workdir: Path):
        command = str(inputs["command"])
        start = time.monotonic()
        independent_step = "shared.txt" not in command
        if independent_step:
            meet.wait()
        time.sleep(0.05)
        out = original(inputs, workdir)
        if independent_step:
            done.wait()
        with guard:
            intervals[command] = (start, time.monotonic())
        return out

    registry._handlers["shell"] = slow_shell  # type: ignore[attr-defined]
    tools = _approved_runtime(registry)
    applicator = SkillApplicator(tools, workspaces, max_parallel_steps=4)

    independent = _skill(
        "parallel-ok",
        [
            Step(
                id="a",
                tool="shell",
                intent="Write file A slowly.",
                inputs={"command": "echo a > A.txt"},
            ),
            Step(
                id="b",
                tool="shell",
                intent="Write file B slowly.",
                inputs={"command": "echo b > B.txt"},
            ),
        ],
        cert_cmd="true",
    )
    conflicting = _skill(
        "serial-needed",
        [
            Step(
                id="w1",
                tool="shell",
                intent="Write shared file first.",
                inputs={"command": "echo 1 > shared.txt"},
                resources=[ResourceClaim(kind="file", id="shared.txt", mode="write")],
            ),
            Step(
                id="w2",
                tool="shell",
                intent="Write shared file second.",
                inputs={"command": "echo 2 > shared.txt"},
                resources=[ResourceClaim(kind="file", id="shared.txt", mode="write")],
            ),
        ],
        cert_cmd="true",
    )

    workdir = tmp_path / "work"
    workdir.mkdir()
    writer = TranscriptWriter(TranscriptStore(tmp_path / "t"), "r", 1)
    try:
        result = applicator.apply(
            independent,
            params={},
            workdir=workdir,
            run_id="r",
            attempt_no=1,
            transcript=writer,
        )
    except threading.BrokenBarrierError:
        pytest.fail(
            f"steps a and b did not run concurrently "
            f"(rendezvous timed out after {barrier_timeout_s} s)"
        )
    assert result.ok
    assert len(result.waves) == 1
    assert set(result.waves[0].wave.step_ids) == {"a", "b"}
    a_start, a_end = intervals["echo a > A.txt"]
    b_start, b_end = intervals["echo b > B.txt"]
    # Implied by the two rendezvous; kept as documentation of the property.
    assert a_start < b_end and b_start < a_end, "independent steps did not overlap"

    workdir2 = tmp_path / "work2"
    workdir2.mkdir()
    writer2 = TranscriptWriter(TranscriptStore(tmp_path / "t2"), "r2", 1)
    result2 = applicator.apply(
        conflicting,
        params={},
        workdir=workdir2,
        run_id="r2",
        attempt_no=1,
        transcript=writer2,
    )
    assert result2.ok
    assert len(result2.waves) == 2, "conflicting write claims must be in separate waves"
    assert result2.waves[0].wave.step_ids == ["w1"]
    assert result2.waves[1].wave.step_ids == ["w2"]
    _, w1_end = intervals["echo 1 > shared.txt"]
    w2_start, _ = intervals["echo 2 > shared.txt"]
    assert w1_end <= w2_start, "conflicting write claims overlapped"


def test_failed_wave_restores_whole_not_half(tmp_path: Path) -> None:
    """A wave that fails mid-flight restores whole rather than leaving a half-applied state."""

    workspaces = WorkspaceManager(tmp_path / "snaps")
    tools = _approved_runtime(default_registry())
    applicator = SkillApplicator(tools, workspaces)

    version = _skill(
        "wave-fail",
        [
            Step(
                id="ok",
                tool="shell",
                intent="Create a partial artifact.",
                inputs={"command": "echo partial > PARTIAL.txt"},
            ),
            Step(
                id="boom",
                tool="shell",
                intent="Fail deliberately.",
                inputs={"command": "exit 1"},
            ),
        ],
        cert_cmd="true",
    )
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "KEPT.txt").write_text("pristine")
    writer = TranscriptWriter(TranscriptStore(tmp_path / "t"), "r", 1)
    result = applicator.apply(
        version,
        params={},
        workdir=workdir,
        run_id="r",
        attempt_no=1,
        transcript=writer,
    )
    assert not result.ok
    assert not (workdir / "PARTIAL.txt").exists(), "wave rollback must remove partial artifact"
    assert (workdir / "KEPT.txt").read_text() == "pristine"


def test_replay_reconstructs_wave_composition_without_model_calls(tmp_path: Path) -> None:
    """Replay tests reconstruct node decisions, including wave composition, with no model calls."""

    workspaces = WorkspaceManager(tmp_path / "snaps")
    tools = _approved_runtime(default_registry())
    applicator = SkillApplicator(tools, workspaces)
    store = TranscriptStore(tmp_path / "t")

    version = _skill(
        "replay-me",
        [
            Step(id="a", tool="shell", intent="Write A.", inputs={"command": "echo a > A.txt"}),
            Step(id="b", tool="shell", intent="Write B.", inputs={"command": "echo b > B.txt"}),
        ],
        cert_cmd="true",
    )
    workdir = tmp_path / "work"
    workdir.mkdir()
    writer = TranscriptWriter(store, "replay-run", 1)
    result = applicator.apply(
        version, params={}, workdir=workdir, run_id="replay-run", attempt_no=1, transcript=writer
    )
    assert result.ok and result.transcript_ref
    recorded = store.read(result.transcript_ref)
    wave_events = [e for e in recorded["events"] if e["kind"] == "wave_start"]
    assert len(wave_events) == 1
    assert set(wave_events[0]["payload"]["step_ids"]) == {"a", "b"}
    # No model events in an apply-only transcript.
    assert not any(e["kind"] == "model" for e in recorded["events"])


def test_bound_step_output_is_the_only_dependency_and_is_recorded(tmp_path: Path) -> None:
    """A data binding creates the edge and leaves evidence in the transcript."""

    registry = ToolRegistry()
    seen: list[dict] = []
    registry.register(
        Tool(name="produce", side_effect="pure"),
        lambda _inputs, _workdir: ToolResult(tool="produce", ok=True, stdout="payload"),
    )

    def consume(inputs: dict, _workdir: Path) -> ToolResult:
        seen.append(inputs)
        return ToolResult(tool="consume", ok=True)

    registry.register(Tool(name="consume", side_effect="pure"), consume)
    applicator = SkillApplicator(ToolRuntime(registry), WorkspaceManager(tmp_path / "snaps"))
    version = _skill(
        "bound-output",
        [
            Step(
                id="produce",
                tool="produce",
                intent="Produce the value for the consumer.",
                outputs=[StepOutput(name="value", type="string")],
            ),
            Step(
                id="consume",
                tool="consume",
                intent="Consume the producer's output value.",
                input_bindings=[
                    InputBinding(input="value", source_step="produce", output="value")
                ],
            ),
        ],
        cert_cmd="true",
    )
    workdir = tmp_path / "work"
    workdir.mkdir()
    store = TranscriptStore(tmp_path / "transcripts")
    result = applicator.apply(
        version,
        params={},
        workdir=workdir,
        run_id="bound-run",
        attempt_no=1,
        transcript=TranscriptWriter(store, "bound-run", 1),
    )

    assert result.ok
    assert [wave.wave.step_ids for wave in result.waves] == [["produce"], ["consume"]]
    assert seen == [{"value": "payload"}]
    events = store.read(result.transcript_ref)["events"]  # type: ignore[arg-type]
    assert any(
        event["kind"] == "step_output"
        and event["payload"] == {
            "step_id": "produce",
            "output": "value",
            "type": "string",
            "value": "payload",
        }
        for event in events
    )
    consume_start = next(
        event
        for event in events
        if event["kind"] == "step_start" and event["payload"]["step_id"] == "consume"
    )
    assert consume_start["payload"]["input_bindings"] == [
        {"input": "value", "source_step": "produce", "output": "value"}
    ]
