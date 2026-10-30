"""Spend charged per attempt, not per run (specs §10.1, §18).

The defect these tests pin down: ``ModelClient.spend``, ``ToolRuntime.invocations`` and
``ClaimScheduler.conflicts`` accumulate for the whole run, so charging an attempt from those
counters directly charges it for every earlier attempt as well — four attempts of two tool
calls each billed twenty. The same read also re-fed every prior invocation into affordance
memory, inflating the failure rates retrieval and failure classification depend on.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.branch import BranchState
from contracts.budget import Budget, BudgetReservation, Spend
from contracts.criteria import SensitivityProof, SkillCertificationCriterion
from contracts.run import RunState, SkillCandidateRef, Task
from contracts.skill import Hygiene, Provenance, SkillVersion, Step
from recertia.governance.sandbox import ApprovalGate
from recertia.graph.ops import OperationLedger
from recertia.ledger import HashChainLedger
from recertia.memory.affordance import AffordanceStore
from recertia.nodes.attempt import AttemptMeter, RuntimeWindow, UsageDelta
from recertia.nodes.context import NodeContext
from recertia.nodes.solve import solve
from recertia.solver.apply import SkillApplicator
from recertia.solver.model import ModelResponse, StubModelClient
from recertia.solver.registry import Tool, ToolResult, default_registry
from recertia.solver.runtime import ToolRuntime, active_model
from recertia.solver.transcript import TranscriptStore
from recertia.workspace import WorkspaceManager

NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)
_OPEN_AFFORDANCE_STORES: list[AffordanceStore] = []


@pytest.fixture(autouse=True)
def _close_affordance_stores() -> Iterator[None]:
    """Close each harness's affordance store at test end, as ``GraphOrchestrator.close()`` does."""

    yield
    while _OPEN_AFFORDANCE_STORES:
        _OPEN_AFFORDANCE_STORES.pop().close()


def _skill(*commands: str) -> SkillVersion:
    return SkillVersion(
        skill_id="metering-skill",
        version=1,
        title="Skill used by attempt metering tests",
        intent="A skill whose steps run trivial shell commands so spend can be counted exactly.",
        task_class="repo-chore",
        steps=[
            Step(id=f"s{i}", intent="run a trivial command", tool="shell", inputs={"command": cmd})
            for i, cmd in enumerate(commands)
        ],
        certification_criteria=[
            SkillCertificationCriterion(
                id="done",
                kind="command",
                run="true",
                weight=1.0,
                preregistered=True,
                sensitivity_proof=SensitivityProof(
                    criterion_id="done", negative_fixture="neg", rejected=True, checked_at=NOW
                ),
            )
        ],
        provenance=Provenance(
            distilled_from_run="metering",
            distilled_at=NOW,
            curation="human_authored",
            derivation="hand_authored",
        ),
        hygiene=Hygiene(secret_scan="passed", scanned_at=NOW),
    )


class _FixedSkillStore:
    def __init__(self, version: SkillVersion) -> None:
        self._version = version

    def get_version(self, skill_id: str, version: int) -> SkillVersion:
        return self._version


class _Harness:
    """One run's worth of services, shared across attempts exactly as the orchestrator shares them."""

    def __init__(self, tmp_path: Path, *, version: SkillVersion, extra_tools: bool = False) -> None:
        self.workdir = tmp_path / "work"
        self.workdir.mkdir()
        registry = default_registry()
        if extra_tools:
            registry.register(
                Tool(name="model_probe", side_effect="read", description="spends model tokens"),
                _model_probe_handler,
            )
        gate = ApprovalGate()
        for name in registry.names():
            gate.approve(name, actor="metering-test")
        self.model = StubModelClient(responses=["true"] * 40, provider="stub", model_id="stub")
        self.tools = ToolRuntime(registry, approval_gate=gate, model=self.model)
        self.workspaces = WorkspaceManager(tmp_path / "snaps")
        self.applicator = SkillApplicator(self.tools, self.workspaces)
        self.affordances = AffordanceStore(tmp_path / "affordance.json")
        _OPEN_AFFORDANCE_STORES.append(self.affordances)
        self.transcripts = TranscriptStore(tmp_path / "transcripts")
        self.ledger = HashChainLedger(tmp_path / "ledger.jsonl")
        self.ops = OperationLedger(tmp_path / "ops.db")
        self.store = _FixedSkillStore(version)

    def ctx(self, attempt_no: int, *, script: list[str] | None = None) -> NodeContext:
        return NodeContext(
            run_id="metering",
            attempt_no=attempt_no,
            node="solve",
            workdir=self.workdir,
            workspaces=self.workspaces,
            ledger=self.ledger,
            ops=self.ops,
            script=script,
            tools=self.tools,
            model=self.model,
            transcripts=self.transcripts,
            applicator=self.applicator,
            affordances=self.affordances,
            store=self.store,  # type: ignore[arg-type]
        )


def _model_probe_handler(inputs: dict, workdir: Path) -> ToolResult:
    """A tool that spends model tokens, the way ``agent_subtask`` does."""

    model = active_model()
    if model is not None:
        model.complete("probe")
    return ToolResult(tool="model_probe", ok=True, exit_code=0)


def _state(**overrides: object) -> RunState:
    base = RunState(
        run_id="metering",
        task=Task(task_id="t", request="count spend", task_class="repo-chore", submitted_at=NOW),
        budget=Budget(max_attempts=8, max_tool_calls=200),
        strategy="apply",
        chosen=SkillCandidateRef(skill_id="metering-skill", version=1, score=0.9),
    )
    return base.model_copy(update=dict(overrides))


def test_repeated_attempts_charge_only_their_own_tool_calls(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, version=_skill("true", "true"))
    state = _state()

    for attempt in range(1, 5):
        state = solve(state, harness.ctx(attempt)).state
        assert state.spent.tool_calls == len(harness.tools.invocations), (
            f"after attempt {attempt}, charged {state.spent.tool_calls} tool calls for "
            f"{len(harness.tools.invocations)} real invocations"
        )

    assert state.spent.tool_calls == 8
    assert state.spent.attempts == 4


def test_repeated_attempts_do_not_re_record_affordances(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, version=_skill("true", "true"))
    state = _state()

    for attempt in range(1, 5):
        state = solve(state, harness.ctx(attempt)).state

    shell = harness.affordances.tool("shell")
    assert shell is not None
    assert shell.invocations == len(harness.tools.invocations) == 8


def test_model_tokens_spent_inside_an_attempt_are_charged_once(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, version=_skill("true"), extra_tools=True)
    harness.applicator = SkillApplicator(harness.tools, harness.workspaces)
    version = _skill("true").model_copy(
        update={
            "steps": [
                Step(id="s0", intent="spend model tokens through a tool", tool="model_probe")
            ]
        }
    )
    harness.store = _FixedSkillStore(version)
    state = _state()

    for attempt in range(1, 4):
        state = solve(state, harness.ctx(attempt)).state
        assert state.spent.tokens == harness.model.spend.tokens

    assert state.spent.tokens > 0


def test_every_solve_path_charges_wall_clock(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, version=_skill("true"))
    outcome = solve(_state(), harness.ctx(1))

    assert outcome.state.spent.wall_clock_s > 0.0


def test_scripted_path_charges_commands_that_ran_before_budget_ran_out(tmp_path: Path) -> None:
    """Work already done is charged even when the attempt ends on an exhausted budget."""

    harness = _Harness(tmp_path, version=_skill("true"))
    state = _state(strategy="scratch", chosen=None, budget=Budget(max_tool_calls=1))
    outcome = solve(state, harness.ctx(1, script=["true", "true"]))

    assert outcome.route == "pre_validation_failure_signal"
    assert outcome.state.spent.tool_calls == 1
    assert outcome.state.spent.attempts == 1
    assert outcome.state.attempt_no == 1


def test_replayed_operations_charge_the_same_as_the_original_attempt(tmp_path: Path) -> None:
    """Resume re-enters solve with pre-solve spend; memoised ops must still be charged."""

    harness = _Harness(tmp_path, version=_skill("true"))
    state = _state(strategy="scratch", chosen=None)

    first = solve(state, harness.ctx(1, script=["true", "true"]))
    replayed = solve(state, harness.ctx(1, script=["true", "true"]))

    assert first.state.spent.tool_calls == replayed.state.spent.tool_calls == 2
    assert len(harness.tools.invocations) == 2, "a replayed op must not re-invoke the tool"


def test_timed_out_branch_reconciles_every_dimension_into_parent_spend(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, version=_skill("true"))
    branch = BranchState(
        branch_id="metering-p0",
        kind="portfolio",
        strategy="scratch",
        workspace_ref=str(harness.workdir / "metering-p0"),
        budget=Budget(max_tool_calls=1, max_tokens=1),
        spent=Spend(tokens=500, tool_calls=1),
        status="dispatched",
    )
    state = _state(strategy="portfolio", chosen=None, branches=[branch])

    outcome = solve(state, harness.ctx(1, script=["true"]))

    assert outcome.state.branches[0].status == "timed_out"
    assert outcome.state.spent.tokens == 500, "branch tokens were dropped on reconciliation"


class TestAttemptMeter:
    def test_preflight_counts_committed_reserved_and_in_attempt_usage(self) -> None:
        meter = AttemptMeter(
            budget=Budget(max_tool_calls=10),
            committed=Spend(tool_calls=4),
            reserved=BudgetReservation(tool_calls=3),
        )
        meter.charge(tool_calls=2)

        assert meter.preflight(tool_calls=1) is None
        assert meter.preflight(tool_calls=2) == "tool_calls"

    def test_wall_clock_budget_is_enforceable(self) -> None:
        ticks = iter([0.0, 100.0, 100.0])
        meter = AttemptMeter(
            budget=Budget(max_wall_clock_s=60),
            committed=Spend(),
            clock=lambda: next(ticks),
        )

        assert meter.preflight() == "wall_clock_s"

    def test_commit_folds_every_dimension_into_run_spend(self) -> None:
        ticks = iter([0.0, 2.5])
        meter = AttemptMeter(
            budget=Budget(),
            committed=Spend(attempts=1, tool_calls=3, tokens=10, cost_usd=0.5, wall_clock_s=1.0),
            clock=lambda: next(ticks),
        )
        meter.charge_delta(UsageDelta(tool_calls=2, tokens=7, cost_usd=0.25))

        spent = meter.commit()

        assert spent.attempts == 2
        assert spent.tool_calls == 5
        assert spent.tokens == 17
        assert spent.cost_usd == pytest.approx(0.75)
        assert spent.wall_clock_s == pytest.approx(3.5)

    def test_open_can_disown_a_reservation_being_retired(self) -> None:
        state = _state(reserved=BudgetReservation(tool_calls=99))

        assert AttemptMeter.open(state).reserved.tool_calls == 99
        assert AttemptMeter.open(state, reserved=BudgetReservation()).reserved.tool_calls == 0


class TestRuntimeWindow:
    def test_reports_deltas_not_cumulative_totals(self, tmp_path: Path) -> None:
        harness = _Harness(tmp_path, version=_skill("true"))
        ctx = harness.ctx(1)
        harness.tools.invoke(
            "shell", {"command": "true"}, workdir=harness.workdir, step_id="before"
        )

        window = RuntimeWindow(ctx)
        harness.tools.invoke(
            "shell", {"command": "true"}, workdir=harness.workdir, step_id="inside"
        )

        assert window.delta().tool_calls == 1
        assert len(window.new_invocations()) == 1
        assert len(harness.tools.invocations) == 2

    def test_usage_delta_survives_a_persisted_operation_result(self) -> None:
        delta = UsageDelta(tool_calls=2, tokens=11, cost_usd=0.125)

        assert UsageDelta.from_dict(delta.as_dict()) == delta
        assert UsageDelta.from_dict({}) == UsageDelta()

    def test_model_only_window_charges_tokens_without_tool_calls(self) -> None:
        model = StubModelClient(responses=["one"], provider="stub", model_id="stub")

        class _ModelOnlyCtx:
            def __init__(self) -> None:
                self.model = model
                self.tools = None

        window = RuntimeWindow(_ModelOnlyCtx())  # type: ignore[arg-type]
        response: ModelResponse = model.complete("prompt")

        delta = window.delta()
        assert delta.tool_calls == 0
        assert delta.tokens == response.prompt_tokens + response.completion_tokens
