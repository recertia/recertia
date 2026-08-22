"""ADR-0019: external trajectory import rejects incomplete provenance and never promotes."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from contracts.goal import Goal, compile_goal
from contracts.policy import IsolationSettings, Policy
from contracts.trajectory_import import TrajectoryImport
from recertia.evals.golden import list_goldens_for_task_class
from recertia.evals.task_classes import COMPUTER_USE_TASK_CLASSES
from recertia.ops.operator_brief import brief_from_events
from recertia.policy_load import load_policy
from recertia.telemetry import SpanEvent
from recertia.trajectory.import_store import ImportRejected, ingest_trajectory


def _valid_payload(**overrides: object) -> dict:
    base = {
        "import_id": "imp-001",
        "source": "synthetic",
        "source_ref": "fixture://repro",
        "captured_at": datetime(2026, 8, 22, tzinfo=timezone.utc).isoformat(),
        "environment": {
            "os": "linux",
            "network_policy": "none",
            "tools": ["browser"],
        },
        "steps": [{"seq": 0, "action": "open", "tool": "browser", "observation": "login", "ok": True}],
        "outcome": "solved",
        "criteria_snapshot": [],
        "provenance": {
            "actor": "tester",
            "captured_at": datetime(2026, 8, 22, tzinfo=timezone.utc).isoformat(),
        },
        "artifacts": [],
        "reexecutable": False,
        "task_class": "bug_reproduction",
    }
    base.update(overrides)
    return base


def test_import_rejects_missing_environment() -> None:
    payload = _valid_payload()
    del payload["environment"]
    with pytest.raises(ValidationError):
        TrajectoryImport.model_validate(payload)


def test_import_rejects_blank_actor() -> None:
    payload = _valid_payload()
    payload["provenance"] = {
        "actor": " ",
        "captured_at": datetime(2026, 8, 22, tzinfo=timezone.utc).isoformat(),
    }
    with pytest.raises(ValidationError):
        TrajectoryImport.model_validate(payload)


def test_import_writes_episodic_and_does_not_promote(tmp_path: Path) -> None:
    result = ingest_trajectory(_valid_payload(), runs_root=tmp_path, tenant_id="default")
    assert result.promoted is False
    assert result.proposal_id is None
    assert (tmp_path / "imports" / "imp-001.json").exists()
    from recertia.memory.episodic import EpisodicStore

    store = EpisodicStore(tmp_path / "runs" / "default" / "episodic")
    rec = store.get_by_case_id("import-imp-001")
    assert rec is not None
    assert rec.approach == "external:synthetic"
    assert rec.outcome == "solved"


def test_reexecutable_queues_pending_proposal_not_approved(tmp_path: Path) -> None:
    payload = _valid_payload(import_id="imp-002", reexecutable=True)
    result = ingest_trajectory(payload, runs_root=tmp_path)
    assert result.promoted is False
    assert result.proposal_id is not None
    from recertia.proposals.store import ProposalStore

    store = ProposalStore(tmp_path / "proposals.sqlite")
    try:
        rec = store.get(result.proposal_id, tenant_id="default")
        assert rec is not None
        assert rec.kind == "external_trajectory"
        assert rec.status == "pending"
        assert rec.payload.get("promoted") is False
    finally:
        store.close()


def test_duplicate_import_rejected(tmp_path: Path) -> None:
    ingest_trajectory(_valid_payload(), runs_root=tmp_path)
    with pytest.raises(ImportRejected, match="already exists"):
        ingest_trajectory(_valid_payload(), runs_root=tmp_path)


def test_flag_off_rejects(tmp_path: Path) -> None:
    policy = load_policy()
    policy = policy.model_copy(
        update={"improvement": policy.improvement.model_copy(update={"external_trajectory_import": False})}
    )
    with pytest.raises(ImportRejected, match="external_trajectory_import"):
        ingest_trajectory(_valid_payload(), runs_root=tmp_path, policy=policy)


def test_computer_use_goldens_compile(tmp_path: Path) -> None:
    root = Path("evals/golden")
    if not root.exists():
        pytest.skip("golden tree not at cwd")
    for task_class in COMPUTER_USE_TASK_CLASSES:
        found = list_goldens_for_task_class(root, task_class)
        assert found, f"missing goldens for {task_class}"
        for golden in found:
            goal = Goal.model_validate_json((golden / "goal.json").read_text(encoding="utf-8"))
            assert goal.task_class == task_class
            criteria = compile_goal(goal, source="caller")
            assert criteria
            assert (golden / "workspace").is_dir()


def test_isolation_defaults_forbid_long_lived_computer() -> None:
    policy = load_policy()
    assert policy.isolation.allow_external_computer is False
    assert policy.improvement.long_lived_computer_backend is False
    assert policy.job_quota.computer_use_practice_share == 0.15
    IsolationSettings()
    Policy.model_validate(policy.model_dump())


def test_operator_brief_honest_when_no_lift() -> None:
    events = [
        SpanEvent(name="tool.invoked", attributes={"canonical_key": "a"}),
        SpanEvent(name="tool.invoked", attributes={"canonical_key": "a"}),
    ]
    brief = brief_from_events(events, task_classes=["bug_reproduction"])
    assert brief.lift_by_task_class[0].established is False
    assert "not established" in brief.lift_by_task_class[0].detail
    assert brief.redundancy["tool_redundancy_rate"] == 0.5
