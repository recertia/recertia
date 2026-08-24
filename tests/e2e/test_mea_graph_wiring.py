"""MEA graph-engine wiring: intake bind, validate-as-auditor, default-off identity.

Maps onto the existing 15-node graph. No new nodes. The synthetic two-criterion
Goal matches tests/fixtures/mea/synthetic_multiphase_state_v0.json.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from contracts.goal import DesiredState, Goal
from contracts.policy import ImprovementFlags, Policy
from contracts.run import Task
from recertia.graph.engine import GraphOrchestrator
from recertia.mea.store import AuditedStateStore


def _multiphase_goal(*, opt_in: bool) -> Goal:
    return Goal(
        goal_id="g_synthetic_multiphase",
        desired=[
            DesiredState(id="phase1_file", kind="file_exists", path="phase1.done"),
            DesiredState(id="phase2_file", kind="file_exists", path="phase2.done"),
        ],
        context="Synthetic multi-phase Goal for MEA lift-harness readiness",
        task_class="repo-chore",
        mea_opt_in=opt_in,
    )


def _script() -> list[str]:
    return [
        "python3 -c \"open('phase1.done','w').write('ok'); open('phase2.done','w').write('ok')\""
    ]


def test_default_path_writes_neither_sidecar_nor_fallback(tmp_path: Path) -> None:
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    runs_root = tmp_path / "runs"
    orch = GraphOrchestrator(runs_root)
    try:
        state = orch.start(
            "run-default",
            Task(
                task_id="t-default",
                goal=_multiphase_goal(opt_in=False),
                submitted_at=datetime.now(timezone.utc),
            ),
            [],
            workdir=workdir,
            script=_script(),
        )
        events = orch.trajectories.list_events("run-default")
        ledger_actions = [e.action for e in orch.ledger.entries()]
    finally:
        orch.close()

    assert state.terminal == "solved"
    assert state.mea_active is False
    assert state.mea_fallback_reason is None
    assert not (runs_root / "audited_states").exists()
    assert all(e.event_kind != "audited_state_delta" for e in events)
    assert "mea_activation_fallback" not in ledger_actions


def test_mea_multiphase_golden_writes_sidecar_and_delta(tmp_path: Path) -> None:
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    runs_root = tmp_path / "runs"
    policy = Policy(
        version="test",
        authoring_prior_version="ap-test",
        improvement=ImprovementFlags(mea_enabled=True),
    )
    orch = GraphOrchestrator(runs_root, policy=policy)
    try:
        state = orch.start(
            "run-mea",
            Task(
                task_id="t-mea",
                goal=_multiphase_goal(opt_in=True),
                execution_strategy="mea",
                submitted_at=datetime.now(timezone.utc),
                is_eval_fixture=True,
            ),
            [],
            workdir=workdir,
            script=_script(),
        )
        events = orch.trajectories.list_events("run-mea")
        ledger_actions = [e.action for e in orch.ledger.entries()]
        audited = orch.audited_states.load("run-mea")
    finally:
        orch.close()

    assert state.terminal == "solved"
    assert state.mea_active is True
    assert state.mea_fallback_reason is None
    assert audited is not None
    assert audited.version >= 1
    assert audited.current_phase == "complete"
    assert {d.decision_id for d in audited.verified_decisions} == {
        "phase1_file",
        "phase2_file",
    }
    assert any(e.event_kind == "audited_state_delta" for e in events)
    assert "mea_activation_fallback" not in ledger_actions
    # Resume from the same runs_root must see the sidecar.
    store = AuditedStateStore(runs_root / "audited_states")
    assert store.load("run-mea") is not None and store.load("run-mea").version == audited.version


def test_incomplete_activation_notes_ledger_not_sidecar(tmp_path: Path) -> None:
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    runs_root = tmp_path / "runs"
    orch = GraphOrchestrator(runs_root)  # policy layer off
    try:
        state = orch.start(
            "run-fallback",
            Task(
                task_id="t-fallback",
                goal=_multiphase_goal(opt_in=True),
                execution_strategy="mea",
                submitted_at=datetime.now(timezone.utc),
            ),
            [],
            workdir=workdir,
            script=_script(),
        )
        events = orch.trajectories.list_events("run-fallback")
        entries = orch.ledger.entries()
    finally:
        orch.close()

    assert state.terminal == "solved"
    assert state.mea_active is False
    assert state.mea_fallback_reason == "policy_mea_disabled"
    assert orch.audited_states.load("run-fallback") is None
    assert any(e.action == "mea_activation_fallback" for e in entries)
    assert all(e.event_kind != "audited_state_delta" for e in events)


def test_graph_stays_fifteen_nodes() -> None:
    from contracts.graph import NODES
    from recertia.nodes import NODE_FUNCS

    assert len(NODES) == 15
    assert len(NODE_FUNCS) == 15
    assert set(NODE_FUNCS) == set(NODES)
