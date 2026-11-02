"""Startup / bootstrap correctness: task_class, wiring, backend, retrieve query."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from contracts.goal import DesiredState, Goal
from contracts.run import RunState, Task
from recertia.bootstrap import build_default_orchestrator, resolve_task_class
from recertia.cli.main import app
from recertia.graph.ops import OperationLedger
from recertia.ledger import HashChainLedger
from recertia.nodes.context import NodeContext
from recertia.nodes.retrieve import retrieval_query, retrieve
from recertia.solver.container import ensure_execution_ready
from recertia.solver.sandbox import SandboxError
from recertia.workspace import WorkspaceManager

runner = CliRunner()


def test_resolve_task_class_prefers_goal_over_default() -> None:
    assert (
        resolve_task_class(explicit=None, goal_task_class="research-synthesis")
        == "research-synthesis"
    )
    assert resolve_task_class(explicit="repo-chore", goal_task_class="research-synthesis") == "repo-chore"
    assert resolve_task_class(explicit=None, goal_task_class=None) == "repo-chore"


def test_api_goal_task_class_not_masked_by_default(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from recertia.api import create_app

    app_api = create_app(root=tmp_path / "api-root", skills_root=tmp_path / "skills")
    issued = app_api.state.api_keys.issue(tenant_id="t1", scopes={"runs", "exec"}, actor="test")
    client = TestClient(app_api)

    created = client.post(
        "/v1/runs",
        json={
            "goal": {
                "desired": [{"id": "f", "kind": "file_exists", "path": "output.txt"}],
                "context": "write output.txt",
                "task_class": "research-synthesis",
            },
            "run_id": "goal-class-1",
            "script": ["python3 -c \"open('output.txt','w').write('done')\""],
            "budget": {"max_attempts": 2},
        },
        headers={"X-API-Key": issued.secret},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["task_class"] == "research-synthesis"
    assert body["has_goal"] is True
    assert body["terminal"] == "solved"


def test_build_default_orchestrator_wires_memory_and_tools(tmp_path: Path) -> None:
    bundle = build_default_orchestrator(
        tmp_path / "runs",
        skills_root=tmp_path / "skills",
        facts_root=tmp_path / "facts",
    )
    try:
        orch = bundle.orchestrator
        assert orch.store is not None
        assert orch.retriever is not None
        assert orch.tools is not None
        assert orch.applicator is not None
        assert orch.episodic is not None
        assert orch.affordances is not None
        assert orch.facts is not None
        assert orch.transcripts is not None
    finally:
        bundle.close()


def test_ensure_execution_ready_fails_clearly_without_container(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "container")
    monkeypatch.setattr("recertia.solver.container.container_runtime", lambda: None)
    with pytest.raises(SandboxError, match="RECERTIA_EXECUTION_BACKEND=local"):
        ensure_execution_ready()


def test_cli_run_without_backend_exits_with_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RECERTIA_EXECUTION_BACKEND", raising=False)
    monkeypatch.setattr("recertia.solver.container.container_runtime", lambda: None)
    spec = tmp_path / "spec.json"
    spec.write_text('{"task":{"request":"x"},"budget":{"max_attempts":1},"script":["true"]}')
    result = runner.invoke(
        app,
        ["run", "--spec", str(spec), "--runs-root", str(tmp_path / "runs"), "--run-id", "no-docker"],
    )
    assert result.exit_code == 2
    assert "Docker or Podman" in result.output or "RECERTIA_EXECUTION_BACKEND=local" in result.output


def test_cli_local_exec_solves_scripted_goal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECERTIA_EXECUTION_BACKEND", raising=False)
    monkeypatch.setattr("recertia.solver.container.container_runtime", lambda: None)
    goal = {
        "desired": [{"id": "f", "kind": "file_exists", "path": "output.txt"}],
        "context": "write output.txt",
        "task_class": "repo-chore",
    }
    spec = {
        "goal": goal,
        "script": ["python3 -c \"open('output.txt','w').write('done')\""],
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(__import__("json").dumps(spec))
    result = runner.invoke(
        app,
        [
            "run",
            "--spec",
            str(spec_path),
            "--runs-root",
            str(tmp_path / "runs"),
            "--skills-root",
            str(tmp_path / "skills"),
            "--facts-root",
            str(tmp_path / "facts"),
            "--local-exec",
            "--run-id",
            "local-ok",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "terminal=solved" in result.output


def test_retrieve_goal_only_without_request_does_not_pass_none(tmp_path: Path) -> None:
    class FakeRetriever:
        def snapshot_id(self) -> str:
            return "snap"

        def search(self, query, **kwargs):
            assert isinstance(query, str)
            assert query  # goal terms fallback
            from contracts.run import MemoryBundle

            return MemoryBundle(), type("E", (), {"snapshot_id": "snap", "dropped": []})()

    goal = Goal(desired=[DesiredState(id="out", kind="file_exists", path="output.txt")])
    task = Task(task_id="t", goal=goal, submitted_at=datetime.now(timezone.utc))
    state = RunState(run_id="r", task=task, criteria=[])
    with closing(OperationLedger(tmp_path / "ops.db")) as ops:
        ctx = NodeContext(
            run_id="r",
            attempt_no=0,
            node="retrieve",
            workdir=tmp_path,
            workspaces=WorkspaceManager(tmp_path / "snaps"),
            ledger=HashChainLedger(tmp_path / "ledger.jsonl"),
            ops=ops,
            retriever=FakeRetriever(),
        )
        outcome = retrieve(state, ctx)
        assert outcome.route == "always"
        assert outcome.state.bundle is not None


def test_retrieval_query_helpers() -> None:
    assert retrieval_query(request="hi", goal_context=None) == "hi"
    assert retrieval_query(request=None, goal_context="ctx") == "ctx"
    assert retrieval_query(request=None, goal_context=None, goal_terms="a b") == "a b"
    assert retrieval_query(request=None, goal_context=None) == ""
