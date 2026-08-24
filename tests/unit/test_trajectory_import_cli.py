"""Phase 0: recertia trajectory import rejects incomplete provenance and never promotes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from recertia.cli.main import app
from recertia.memory.episodic import EpisodicStore
from recertia.trajectory.import_store import ImportRejected, ingest_trajectory

runner = CliRunner()


def _valid_payload(**overrides: object) -> dict:
    base: dict = {
        "import_id": "imp-001",
        "source": "synthetic",
        "source_ref": "fixture://repro",
        "captured_at": datetime(2026, 8, 23, tzinfo=timezone.utc).isoformat(),
        "environment": {"os": "linux", "tools": ["browser"], "network_policy": "none"},
        "steps": [{"seq": 0, "action": "open", "target": "/login", "observed": "form"}],
        "outcome": "solved",
        "criteria_snapshot": [],
        "provenance": {"source": "synthetic", "source_ref": "fixture://repro"},
        "artifacts": [],
        "reexecutable": False,
        "require_auditor_reverify": True,
    }
    base.update(overrides)
    return base


def test_ingest_writes_episodic_and_does_not_promote(tmp_path: Path) -> None:
    result = ingest_trajectory(_valid_payload(), runs_root=tmp_path, tenant_id="default")
    assert result.promoted is False
    assert result.may_promote is False
    assert result.promote_reason == "not_reexecutable"
    assert (tmp_path / "runs" / "default" / "imports" / "imp-001.json").exists()
    rec = EpisodicStore(tmp_path / "runs" / "default" / "episodic").get_by_case_id(
        "import-imp-001"
    )
    assert rec is not None
    assert rec.approach == "external:synthetic"
    assert rec.outcome == "solved"


def test_reexecutable_still_does_not_promote(tmp_path: Path) -> None:
    payload = _valid_payload(import_id="imp-002", reexecutable=True)
    result = ingest_trajectory(payload, runs_root=tmp_path)
    assert result.promoted is False
    assert result.may_promote is False
    assert result.promote_reason == "missing_criteria_snapshot"


def test_duplicate_import_rejected(tmp_path: Path) -> None:
    ingest_trajectory(_valid_payload(), runs_root=tmp_path)
    with pytest.raises(ImportRejected, match="already exists"):
        ingest_trajectory(_valid_payload(), runs_root=tmp_path)


def test_import_id_path_escape_rejected(tmp_path: Path) -> None:
    with pytest.raises(ImportRejected, match="import_id"):
        ingest_trajectory(_valid_payload(import_id="../etc/passwd"), runs_root=tmp_path)
    with pytest.raises(ImportRejected, match="import_id"):
        ingest_trajectory(_valid_payload(import_id="imp/001"), runs_root=tmp_path)


def test_tenants_do_not_share_import_files(tmp_path: Path) -> None:
    ingest_trajectory(_valid_payload(import_id="imp-t"), runs_root=tmp_path, tenant_id="a")
    ingest_trajectory(_valid_payload(import_id="imp-t"), runs_root=tmp_path, tenant_id="b")
    assert (tmp_path / "runs" / "a" / "imports" / "imp-t.json").exists()
    assert (tmp_path / "runs" / "b" / "imports" / "imp-t.json").exists()


def test_cli_import_rejects_and_accepts(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    denied = runner.invoke(app, ["trajectory", "import", str(bad), "--runs-root", str(tmp_path)])
    assert denied.exit_code == 1
    combined = (denied.output + denied.stdout + denied.stderr).lower()
    assert "rejected" in combined or "ok" in combined

    empty_env = tmp_path / "empty-env.json"
    empty_env.write_text(
        json.dumps(_valid_payload(import_id="imp-empty", environment={"network_policy": "none"})),
        encoding="utf-8",
    )
    env_denied = runner.invoke(
        app, ["trajectory", "import", str(empty_env), "--runs-root", str(tmp_path)]
    )
    assert env_denied.exit_code == 1

    good = tmp_path / "good.json"
    good.write_text(json.dumps(_valid_payload(import_id="imp-cli")), encoding="utf-8")
    ok = runner.invoke(app, ["trajectory", "import", str(good), "--runs-root", str(tmp_path)])
    assert ok.exit_code == 0, ok.output
    body = json.loads(ok.output)
    assert body["ok"] is True
    assert body["promoted"] is False
    assert body["import_id"] == "imp-cli"


def _distillable(**overrides: object) -> dict:
    payload = _valid_payload(
        import_id="imp-d",
        reexecutable=True,
        steps=[
            {
                "seq": 0,
                "action": "python -c \"open('x','w').write('ok')\"",
                "input": "python -c \"open('x','w').write('ok')\"",
            }
        ],
        criteria_snapshot=[
            {
                "id": "x-exists",
                "kind": "command",
                "run": "test -f x",
                "source": "caller",
                "weight": 1.0,
            }
        ],
    )
    payload.update(overrides)
    return payload


def test_reexecutable_with_criteria_queues_pending_proposal(tmp_path: Path) -> None:
    result = ingest_trajectory(_distillable(), runs_root=tmp_path)
    assert result.promoted is False
    assert result.may_promote is True
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


def test_distill_imported_never_promotes(tmp_path: Path) -> None:
    from contracts.trajectory_import import TrajectoryImport
    from recertia.distill.imported import DistillRejected, distill_imported
    from recertia.memory.procedural.store import SkillStore

    store = SkillStore(tmp_path / "skills")
    frozen = TrajectoryImport.model_validate(_valid_payload(reexecutable=False))
    with pytest.raises(DistillRejected):
        distill_imported(frozen, store, task_class="bug_reproduction")

    browser_only = TrajectoryImport.model_validate(
        _valid_payload(import_id="imp-browser", reexecutable=True)
    )
    with pytest.raises(DistillRejected, match="shell steps|command criterion"):
        distill_imported(browser_only, store, task_class="bug_reproduction")

    imported = TrajectoryImport.model_validate(_distillable())
    version = distill_imported(imported, store, task_class="bug_reproduction")
    status = store.get_status(version.skill_id, version.version)
    assert version.skill_id == "import-imp-d"
    assert version.task_class == "bug-reproduction"
    assert status is not None
    assert status.lifecycle == "candidate"
    assert status.active is False
    assert version.steps[0].inputs["command"] != "true"


def test_distill_rejects_unknown_task_class(tmp_path: Path) -> None:
    from contracts.trajectory_import import TrajectoryImport
    from recertia.distill.imported import DistillRejected, distill_imported
    from recertia.memory.procedural.store import SkillStore

    with pytest.raises(DistillRejected, match="computer-use"):
        distill_imported(
            TrajectoryImport.model_validate(_distillable()),
            SkillStore(tmp_path / "s"),
            task_class="repo-chore",
        )


def test_cli_distill_authors_candidate(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_distillable(import_id="imp-cli-d")), encoding="utf-8")
    skills = tmp_path / "skills"
    ok = runner.invoke(
        app,
        [
            "trajectory",
            "distill",
            str(good),
            "--skills-root",
            str(skills),
            "--task-class",
            "bug_reproduction",
        ],
    )
    assert ok.exit_code == 0, ok.output
    body = json.loads(ok.output)
    assert body["ok"] is True
    assert body["promoted"] is False
    assert body["lifecycle"] == "candidate"
    assert body["task_class"] == "bug-reproduction"


def test_external_computer_tool_registered_and_refuses_by_default(tmp_path: Path) -> None:
    from recertia.solver.registry import default_registry
    from recertia.solver.result_cache import ToolResultCache

    registry = default_registry()
    assert "external_computer" in registry.names()
    tool = registry.get("external_computer")
    assert tool.side_effect == "external"
    result = registry.handler("external_computer")({"backend": "grok_bot"}, tmp_path)
    assert result.ok is False
    assert "allow_external_computer is false" in result.stderr
    cache = ToolResultCache()
    cache.store(tool, {"backend": "grok_bot"}, result, snapshot_hash="x")
    assert cache.lookup(tool, {"backend": "grok_bot"}, snapshot_hash="x") is None
    assert cache.stats.skipped >= 1


def test_operator_brief_honest_when_no_lift() -> None:
    from recertia.ops.operator_brief import brief_from_events
    from recertia.telemetry import SpanEvent

    events = [
        SpanEvent(name="tool.invoked", attributes={"canonical_key": "a"}),
        SpanEvent(name="tool.invoked", attributes={"canonical_key": "a"}),
    ]
    brief = brief_from_events(events, task_classes=["bug_reproduction"])
    assert brief.lift_by_task_class[0].established is False
    assert "not established" in brief.lift_by_task_class[0].detail
    assert brief.redundancy["tool_redundancy_rate"] == 0.5


def test_no_external_trajectory_import_policy_flag() -> None:
    from recertia.policy_load import load_policy

    policy = load_policy()
    assert not hasattr(policy.improvement, "external_trajectory_import")


def test_ingest_public_dict_never_promotes(tmp_path: Path) -> None:
    result = ingest_trajectory(_valid_payload(import_id="imp-pub"), runs_root=tmp_path)
    public = result.as_public_dict()
    assert public["promoted"] is False
    assert public["import_id"] == "imp-pub"
    assert "stored_path" in public


def test_skill_task_class_is_the_only_snake_to_kebab_map() -> None:
    from contracts.computer_use_goldens import GOLDEN_TASK_CLASSES
    from contracts.policy import COMPUTER_USE_TASK_CLASSES
    from recertia.distill.task_class import computer_use_class_help, skill_task_class

    assert frozenset(COMPUTER_USE_TASK_CLASSES) == frozenset(GOLDEN_TASK_CLASSES)
    assert skill_task_class("bug_reproduction") == "bug-reproduction"
    assert skill_task_class("playtest_operator") == "playtest-operator"
    assert skill_task_class("docs_auditor") == "docs-auditor"
    help_text = computer_use_class_help()
    for name in COMPUTER_USE_TASK_CLASSES:
        assert name in help_text
    assert "bug-reproduction" not in help_text


def test_cli_distill_help_binds_computer_use_task_classes() -> None:
    from contracts.policy import COMPUTER_USE_TASK_CLASSES
    from recertia.cli import trajectory_cmd
    from recertia.distill.task_class import computer_use_class_help

    assert computer_use_class_help() == "|".join(COMPUTER_USE_TASK_CLASSES)
    source = Path(trajectory_cmd.__file__).read_text(encoding="utf-8")
    assert "computer_use_class_help()" in source


def test_external_computer_allowlisted_still_opens_no_standing_vm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from recertia.policy_load import load_policy
    from recertia.solver.external_computer import external_computer_handler

    policy = load_policy()
    isolation = policy.isolation.model_copy(
        update={
            "allow_external_computer": True,
            "long_lived_computer_backend": True,
            "external_computer_allowlist": ["grok_bot"],
        }
    )
    monkeypatch.setattr(
        "recertia.policy_load.load_policy",
        lambda: policy.model_copy(update={"isolation": isolation}),
    )
    result = external_computer_handler({"backend": "grok_bot"}, tmp_path)
    assert result.ok is False
    combined = (result.stderr or "").lower()
    assert "standing vm" in combined
    assert "approved state" in combined
