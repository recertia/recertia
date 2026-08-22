"""ADR-0019: external trajectory import rejects incomplete provenance and never promotes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from contracts.goal import Goal, compile_goal
from contracts.policy import IsolationSettings, JobQuota, Policy
from contracts.trajectory_import import TrajectoryImport
from recertia.distill.imported import DistillRejected, distill_imported
from recertia.evals.golden import list_goldens_for_task_class
from recertia.evals.task_classes import COMPUTER_USE_TASK_CLASSES
from recertia.memory.procedural.store import SkillStore
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
        "task_class": "bug-reproduction",
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
    assert (tmp_path / "runs" / "default" / "imports" / "imp-001.json").exists()
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

    store = ProposalStore(tmp_path / "runs" / "default" / "proposals.sqlite")
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


def test_computer_use_goldens_compile() -> None:
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
    brief = brief_from_events(events, task_classes=["bug-reproduction"])
    assert brief.lift_by_task_class[0].established is False
    assert "not established" in brief.lift_by_task_class[0].detail
    assert brief.redundancy["tool_redundancy_rate"] == 0.5


def test_cli_import_rejects_and_accepts(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from recertia.cli.main import app

    runner = CliRunner()
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    denied = runner.invoke(app, ["trajectory", "import", str(bad), "--runs-root", str(tmp_path)])
    assert denied.exit_code == 1
    assert "rejected" in (denied.output + denied.stdout + denied.stderr).lower()

    good = tmp_path / "good.json"
    good.write_text(json.dumps(_valid_payload(import_id="imp-cli")), encoding="utf-8")
    ok = runner.invoke(app, ["trajectory", "import", str(good), "--runs-root", str(tmp_path)])
    assert ok.exit_code == 0, ok.output
    assert '"promoted": false' in ok.output.lower()


def test_computer_use_quota_share() -> None:
    quota = JobQuota(weekly_token_cap=1000, computer_use_practice_share=0.1)
    assert quota.computer_use_remaining() == 100
    assert quota.can_admit("practice_band", task_class="bug-reproduction", tokens=101) is False
    charged = quota.charge("practice_band", 40, task_class="bug-reproduction")
    assert charged.computer_use_tokens_spent == 40
    assert charged.can_admit("practice_band", task_class="docs-auditor", tokens=70) is False
    assert quota.can_admit("practice_band", task_class="repo-chore", tokens=200) is True


def test_import_id_rejects_path_escape() -> None:
    with pytest.raises(ValidationError):
        TrajectoryImport.model_validate(_valid_payload(import_id="../etc/passwd"))
    with pytest.raises(ValidationError):
        TrajectoryImport.model_validate(_valid_payload(import_id="imp/001"))


def test_tenants_do_not_share_import_files(tmp_path: Path) -> None:
    ingest_trajectory(_valid_payload(import_id="imp-t"), runs_root=tmp_path, tenant_id="a")
    ingest_trajectory(_valid_payload(import_id="imp-t"), runs_root=tmp_path, tenant_id="b")
    assert (tmp_path / "runs" / "a" / "imports" / "imp-t.json").exists()
    assert (tmp_path / "runs" / "b" / "imports" / "imp-t.json").exists()


def test_computer_use_quota_exhausted_refuses_zero_token_job() -> None:
    quota = JobQuota(weekly_token_cap=100, computer_use_practice_share=0.1)
    quota = quota.charge("practice_band", 10, task_class="bug-reproduction")
    assert quota.computer_use_remaining() == 0
    assert quota.can_admit("practice_band", task_class="bug-reproduction", tokens=0) is False


def test_distill_imported_never_promotes(tmp_path: Path) -> None:
    frozen = TrajectoryImport.model_validate(_valid_payload(reexecutable=False))
    store = SkillStore(tmp_path / "skills")
    with pytest.raises(DistillRejected):
        distill_imported(frozen, store)
    browser_only = TrajectoryImport.model_validate(
        _valid_payload(import_id="imp-browser", reexecutable=True)
    )
    with pytest.raises(DistillRejected, match="shell steps|command criterion"):
        distill_imported(browser_only, store)
    payload = _valid_payload(
        import_id="imp-d",
        reexecutable=True,
        steps=[
            {"seq": 0, "action": "python -c 'open(\"x\",\"w\").write(\"ok\")'", "tool": "shell", "ok": True}
        ],
        criteria_snapshot=[
            {
                "id": "x-exists",
                "kind": "command",
                "run": "test -f x",
                "source": "caller",
                "weight": 1.0,
                "sensitivity_proof": {
                    "criterion_id": "x-exists",
                    "negative_fixture": "empty",
                    "rejected": True,
                    "checked_at": "2026-08-22T00:00:00Z",
                },
            }
        ],
    )
    imported = TrajectoryImport.model_validate(payload)
    version = distill_imported(imported, store)
    status = store.get_status(version.skill_id, version.version)
    assert version.skill_id == "import-imp-d"
    assert status.lifecycle == "candidate"
    assert status.active is False
    assert version.steps[0].inputs["command"] != "true"


def test_distill_refuses_when_import_flag_off(tmp_path: Path) -> None:
    policy = load_policy()
    policy = policy.model_copy(
        update={"improvement": policy.improvement.model_copy(update={"external_trajectory_import": False})}
    )
    payload = _valid_payload(
        import_id="imp-flag",
        reexecutable=True,
        steps=[{"seq": 0, "action": "echo hi", "tool": "shell", "ok": True}],
        criteria_snapshot=[
            {
                "id": "true-cmd",
                "kind": "command",
                "run": "test -f README.md",
                "source": "caller",
                "weight": 1.0,
                "sensitivity_proof": {
                    "criterion_id": "true-cmd",
                    "negative_fixture": "empty",
                    "rejected": True,
                    "checked_at": "2026-08-22T00:00:00Z",
                },
            }
        ],
    )
    with pytest.raises(DistillRejected, match="external_trajectory_import"):
        distill_imported(TrajectoryImport.model_validate(payload), SkillStore(tmp_path / "s"), policy=policy)
