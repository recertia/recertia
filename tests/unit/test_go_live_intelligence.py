"""Go-live wiring: model factory, fetch/agent_subtask tools, fail-loud scratch, gc/jobs."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from contracts.budget import Budget
from contracts.criteria import TaskCriterion, mint_rejecting_proof
from contracts.run import RunState, Task
from recertia.config import load_model_config
from recertia.governance.sandbox import ApprovalGate
from recertia.retention import garbage_collect
from recertia.solver.factory import ModelConfigError, build_model_client, build_solver_and_verifier
from recertia.solver.model import StubModelClient
from recertia.solver.providers import AnthropicModelClient, OpenAIModelClient
from recertia.solver.tools import ToolRuntime, default_registry

_NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _criterion(cid: str = "ok") -> TaskCriterion:
    base = TaskCriterion(
        id=cid,
        kind="command",
        run="true",
        source="caller",
        weight=1.0,
        preregistered=True,
    )
    return base.model_copy(
        update={"sensitivity_proof": mint_rejecting_proof(base, fingerprint="go-live")}
    )


def test_load_model_config_provider_model_shorthand(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECERTIA_MODEL_PROVIDER", raising=False)
    monkeypatch.delenv("RECERTIA_MODEL_ID", raising=False)
    cfg = load_model_config(model="anthropic:claude-test")
    assert cfg.provider == "anthropic"
    assert cfg.model_id == "claude-test"
    assert cfg.api_key_env == "ANTHROPIC_API_KEY"


def test_build_solver_and_verifier_stub_is_none_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RECERTIA_ALLOW_STUB_MODEL", raising=False)
    monkeypatch.setenv("RECERTIA_MODEL_PROVIDER", "stub")
    solver, verifier = build_solver_and_verifier(load_model_config())
    assert solver is None
    assert verifier is None


def test_build_solver_allows_stub_when_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECERTIA_ALLOW_STUB_MODEL", "1")
    monkeypatch.setenv("RECERTIA_MODEL_PROVIDER", "stub")
    solver, _ = build_solver_and_verifier(load_model_config())
    assert isinstance(solver, StubModelClient)


def test_build_model_client_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = load_model_config(model="openai:gpt-test")
    with pytest.raises(ModelConfigError, match="API key"):
        build_model_client(cfg)


def test_build_openai_and_anthropic_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-test")
    openai = build_model_client(load_model_config(model="openai:gpt-test"))
    anthropic = build_model_client(load_model_config(model="anthropic:claude-test"))
    assert isinstance(openai, OpenAIModelClient)
    assert isinstance(anthropic, AnthropicModelClient)


def test_default_registry_includes_fetch_and_agent_subtask() -> None:
    names = default_registry().names()
    assert "fetch" in names
    assert "agent_subtask" in names


def test_fetch_rejects_non_allowlisted_host(tmp_path: Path) -> None:
    runtime = _approved_runtime()
    result = runtime.invoke(
        "fetch",
        {"url": "https://evil.example/secret"},
        workdir=tmp_path,
        step_id="f1",
    )
    assert not result.ok
    assert "allowlisted" in result.stderr


def test_fetch_package_builds_pypi_url(tmp_path: Path) -> None:
    from recertia.solver.runtime import StepInvokeContext

    runtime = _approved_runtime()
    payload = json.dumps(
        {"info": {"name": "demo", "version": "1.2.3", "summary": "ok"}}
    ).encode()

    with patch("recertia.solver.registry._https_get", return_value=payload):
        result = runtime.invoke(
            "fetch",
            {},
            workdir=tmp_path,
            step_id="f2",
            step_context=StepInvokeContext(params={"package": "demo"}),
        )
    assert result.ok
    assert "1.2.3" in result.stdout


def test_agent_subtask_requires_model(tmp_path: Path) -> None:
    runtime = _approved_runtime(model=None)
    result = runtime.invoke(
        "agent_subtask",
        {"_intent": "fix tests"},
        workdir=tmp_path,
        step_id="a1",
    )
    assert not result.ok
    assert "model" in result.stderr.lower()


def test_agent_subtask_runs_model_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "local")
    model = StubModelClient(responses=["touch REPAIRED"])
    runtime = _approved_runtime(model=model)
    result = runtime.invoke(
        "agent_subtask",
        {"_intent": "create REPAIRED marker"},
        workdir=tmp_path,
        step_id="a2",
    )
    assert not result.ok
    assert "container" in result.stderr
    assert not (tmp_path / "REPAIRED").exists()


def test_scratch_without_model_fails_loud(tmp_path: Path) -> None:
    from recertia.graph.ops import OperationLedger
    from recertia.ledger import HashChainLedger
    from recertia.nodes.context import NodeContext
    from recertia.nodes.solve import solve
    from recertia.solver.transcript import TranscriptStore
    from recertia.workspace import WorkspaceManager

    workdir = tmp_path / "work"
    workdir.mkdir()
    gate = ApprovalGate()
    registry = default_registry()
    for name in registry.names():
        gate.approve(name, actor="test", reason="test")
    tools = ToolRuntime(registry, approval_gate=gate, model=None)
    state = RunState(
        run_id="scratch1",
        task=Task(
            task_id="t",
            request="do something clever",
            task_class="repo-chore",
            submitted_at=_NOW,
        ),
        criteria=[_criterion()],
        budget=Budget(max_attempts=1),
        strategy="scratch",
    )
    ctx = NodeContext(
        run_id="scratch1",
        attempt_no=1,
        node="solve",
        workdir=workdir,
        workspaces=WorkspaceManager(tmp_path / "snaps"),
        ledger=HashChainLedger(tmp_path / "ledger.jsonl"),
        ops=OperationLedger(tmp_path / "ops.db"),
        tools=tools,
        transcripts=TranscriptStore(tmp_path / "transcripts"),
        model=None,
    )
    outcome = solve(state, ctx)
    assert outcome.route == "pre_validation_failure_signal"
    assert outcome.state.failure_signal is not None
    assert "model client" in outcome.state.failure_signal.detail


def test_garbage_collect_removes_old_artifacts(tmp_path: Path) -> None:
    snaps = tmp_path / "snapshots" / "oldrun-attempt0-deadbeef"
    snaps.mkdir(parents=True)
    (snaps / "x.txt").write_text("x")
    old = 1_700_000_000.0  # firmly in the past
    os.utime(snaps, (old, old))
    report = garbage_collect(tmp_path, older_than_days=1.0, dry_run=False)
    assert "oldrun-attempt0-deadbeef" in report.snapshots
    assert not snaps.exists()


def test_jobs_cli_curator_dry_run(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from recertia.cli.main import app
    from recertia.memory.procedural.seeds import SEED_SKILLS, seed_stats, seed_status_draft
    from recertia.memory.procedural.store import SkillStore

    store = SkillStore(tmp_path / "skills")
    version = SEED_SKILLS[0]
    store.write_version(version)
    store.write_status(seed_status_draft(version))
    store.write_stats(seed_stats(version))

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "jobs",
            "run",
            "curator",
            "--skills-root",
            str(tmp_path / "skills"),
            "--runs-root",
            str(tmp_path / "runs"),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["job"] == "curator"
    assert payload["proposals"]


def _approved_runtime(model: StubModelClient | None = None) -> ToolRuntime:
    gate = ApprovalGate()
    registry = default_registry()
    for name in registry.names():
        gate.approve(name, actor="test", reason="test")
    return ToolRuntime(registry, approval_gate=gate, model=model)
