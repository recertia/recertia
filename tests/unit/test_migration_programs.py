"""GP0 migration programs (Goal packs) conformance."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from contracts.budget import Budget
from contracts.goal import Constraint, DesiredState, Goal, compile_goal
from contracts.program import MigrationProgram, MigrationStep, budget_from_goal_constraints
from recertia.api import RunRecord, create_app
from recertia.nodes._util import criteria_hash
from recertia.programs.materialize import (
    MaterializeError,
    assert_freeze_enforcement_allowed,
    assert_gp0_execution_prereqs,
    materialize_step_goal,
    preview_hash,
)
from recertia.programs.stress import stress_step
from tests.support.http import error_text


def _step(
    step_id: str,
    ordinal: int,
    *,
    role: str = "custom",
    path: str = "README.md",
    freeze: list[str] | None = None,
    mutate: list[str] | None = None,
) -> dict:
    return {
        "step_id": step_id,
        "ordinal": ordinal,
        "title": step_id,
        "role": role,
        "goal": {
            "desired": [
                {"id": f"{step_id}-d", "kind": "file_exists", "path": path, "weight": 1.0}
            ],
            "context": f"context for {step_id}",
            "task_class": "repo-chore",
        },
        "freeze_paths": freeze or [],
        "mutate_paths": mutate or [],
    }


def _client(tmp_path: Path) -> tuple[TestClient, Any]:
    app = create_app(root=tmp_path / "api-root", skills_root=tmp_path / "skills")
    return TestClient(app), app


def _issue(app: Any, *, tenant_id: str = "t1") -> dict[str, str]:
    issued = app.state.api_keys.issue(
        tenant_id=tenant_id,
        scopes={"runs", "metrics", "exec", "admin"},
        actor="test",
    )
    return {"X-API-Key": issued.secret}


def test_budget_ceiling_applied_to_run_budget() -> None:
    goal = Goal(
        desired=[DesiredState(id="f", kind="file_exists", path="x")],
        constraints=[Constraint(id="cost", kind="budget_ceiling", value=2.5)],
    )
    b = budget_from_goal_constraints(goal, Budget(max_cost_usd=10.0))
    assert b.max_cost_usd == 2.5


def test_materialize_advisory_freeze_does_not_inject_must_not_modify() -> None:
    step = MigrationStep.model_validate(_step("s1", 0, freeze=["src/api"]))
    prog = MigrationProgram(
        program_id="p1",
        tenant_id="t1",
        title="t",
        freeze_enforcement="advisory",
        steps=[step],
    )
    goal = materialize_step_goal(prog, step)
    assert not any(c.kind == "must_not_modify" for c in goal.constraints)
    warnings = stress_step(prog, step, goal=goal)
    assert any(w.code == "freeze_advisory" for w in warnings)


def test_materialize_hard_freeze_injects_constraint() -> None:
    step = MigrationStep.model_validate(_step("s1", 0, freeze=["src/api"]))
    prog = MigrationProgram(
        program_id="p1",
        tenant_id="t1",
        title="t",
        freeze_enforcement="hard",
        steps=[step],
    )
    goal = materialize_step_goal(prog, step)
    assert any(c.kind == "must_not_modify" for c in goal.constraints)


def test_freeze_mutate_overlap_blocks() -> None:
    step = MigrationStep.model_validate(
        _step("s1", 0, freeze=["src/api"], mutate=["src/api"])
    )
    prog = MigrationProgram(
        program_id="p1", tenant_id="t1", title="t", steps=[step]
    )
    with pytest.raises(MaterializeError, match="freeze_mutate_overlap"):
        materialize_step_goal(prog, step)


def test_program_bar_merge() -> None:
    step = MigrationStep.model_validate(_step("s2", 1, role="structural"))
    prog = MigrationProgram(
        program_id="p1",
        tenant_id="t1",
        title="t",
        steps=[step],
        program_bar_desired=[
            DesiredState(id="bar", kind="command", run="python -m pytest -q", weight=1.0)
        ],
    )
    goal = materialize_step_goal(prog, step)
    assert any(d.id == "bar" for d in goal.desired)


def test_gp0_execution_prereqs() -> None:
    step = MigrationStep.model_validate(_step("s1", 0))
    prog = MigrationProgram(
        program_id="p1", tenant_id="t1", title="t", handoff="none", steps=[step]
    )
    with pytest.raises(MaterializeError, match="workdir"):
        assert_gp0_execution_prereqs(prog, step, workdir=None, plan_only=False)
    assert_gp0_execution_prereqs(prog, step, workdir=None, plan_only=True)


def test_gp_t1_accept_creates_zero_runs(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    created = client.post(
        "/v1/programs",
        headers=headers,
        json={
            "title": "provider port",
            "intent": "extract provider",
            "steps": [
                _step("char", 0, role="characterization"),
                _step("move", 1, role="structural", path="src"),
            ],
        },
    )
    assert created.status_code == 200, created.text
    pid = created.json()["program"]["program_id"]
    accepted = client.post(
        f"/v1/programs/{pid}/accept",
        headers=headers,
        json={"ack_disclaimer": True},
    )
    assert accepted.status_code == 200
    assert accepted.json()["program"]["status"] == "active"
    assert app.state.runs == {}


def test_gp_t5_tenant_isolation(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    ha = _issue(app, tenant_id="tenant-a")
    hb = _issue(app, tenant_id="tenant-b")
    created = client.post(
        "/v1/programs",
        headers=ha,
        json={"title": "a", "steps": [_step("s1", 0)]},
    )
    pid = created.json()["program"]["program_id"]
    denied = client.get(f"/v1/programs/{pid}", headers=hb)
    assert denied.status_code == 404


def test_hard_freeze_allowed_after_sealing() -> None:
    assert_freeze_enforcement_allowed("hard")
    assert_freeze_enforcement_allowed("advisory")


def test_create_allows_hard_freeze(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    created = client.post(
        "/v1/programs",
        headers=headers,
        json={
            "title": "x",
            "freeze_enforcement": "hard",
            "steps": [_step("s1", 0, freeze=["README.md"])],
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["program"]["freeze_enforcement"] == "hard"


def _bind_solved_run(
    client: TestClient,
    app: Any,
    headers: dict[str, str],
    *,
    tenant_id: str,
    program_id: str,
    step_id: str,
    run_id: str,
    idempotency_key: str | None = None,
) -> Any:
    prev = client.post(f"/v1/programs/{program_id}/steps/{step_id}/preview", headers=headers)
    assert prev.status_code == 200, prev.text
    ph = prev.json()["criteria_preview_hash"]
    goal = Goal.model_validate(prev.json()["goal"])
    # Match intake hash algorithm used for bind integrity
    ch = criteria_hash(compile_goal(goal))
    assert ch == ph
    app.state.runs[(tenant_id, run_id)] = RunRecord(
        run_id=run_id,
        request="x",
        task_class="repo-chore",
        status="solved",
        created_at=datetime.now(timezone.utc),
        tenant_id=tenant_id,
        terminal="solved",
        has_goal=True,
        criteria_hash=ph,
    )
    body: dict[str, Any] = {"bind_run_id": run_id, "workdir": "ws"}
    if idempotency_key:
        body["idempotency_key"] = idempotency_key
    return client.post(
        f"/v1/programs/{program_id}/steps/{step_id}/run",
        headers=headers,
        json=body,
    )


def test_gp_t3_bind_run_and_linear_gate(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app, tenant_id="t1")
    created = client.post(
        "/v1/programs",
        headers=headers,
        json={
            "title": "mig",
            "steps": [
                _step("char", 0, role="characterization"),
                _step("move", 1, role="structural"),
            ],
        },
    )
    pid = created.json()["program"]["program_id"]
    client.post(f"/v1/programs/{pid}/accept", headers=headers, json={"ack_disclaimer": True})

    # Second step cannot bind before first succeeds
    app.state.runs[("t1", "run-early")] = RunRecord(
        run_id="run-early",
        request="x",
        task_class="repo-chore",
        status="solved",
        created_at=datetime.now(timezone.utc),
        tenant_id="t1",
        terminal="solved",
        has_goal=True,
        criteria_hash="deadbeef",
    )
    client.post(f"/v1/programs/{pid}/steps/move/preview", headers=headers)
    blocked = client.post(
        f"/v1/programs/{pid}/steps/move/run",
        headers=headers,
        json={"bind_run_id": "run-early", "workdir": "ws"},
    )
    assert blocked.status_code == 409

    bound = _bind_solved_run(
        client,
        app,
        headers,
        tenant_id="t1",
        program_id=pid,
        step_id="char",
        run_id="run-char",
        idempotency_key="k1",
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()["step_status"] == "succeeded"
    again = client.post(
        f"/v1/programs/{pid}/steps/char/run",
        headers=headers,
        json={"bind_run_id": "run-char", "workdir": "ws", "idempotency_key": "k1"},
    )
    assert again.json()["idempotent"] is True

    move = _bind_solved_run(
        client,
        app,
        headers,
        tenant_id="t1",
        program_id=pid,
        step_id="move",
        run_id="run-move",
    )
    assert move.status_code == 200
    assert move.json()["program"]["status"] == "completed"


def test_bind_rejects_mismatched_criteria_hash(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app, tenant_id="t1")
    created = client.post(
        "/v1/programs",
        headers=headers,
        json={"title": "p", "steps": [_step("s1", 0)]},
    )
    pid = created.json()["program"]["program_id"]
    client.post(f"/v1/programs/{pid}/accept", headers=headers, json={"ack_disclaimer": True})
    client.post(f"/v1/programs/{pid}/steps/s1/preview", headers=headers)
    app.state.runs[("t1", "run-bad")] = RunRecord(
        run_id="run-bad",
        request="x",
        task_class="repo-chore",
        status="solved",
        created_at=datetime.now(timezone.utc),
        tenant_id="t1",
        terminal="solved",
        has_goal=True,
        criteria_hash="not-the-preview-hash",
    )
    bad = client.post(
        f"/v1/programs/{pid}/steps/s1/run",
        headers=headers,
        json={"bind_run_id": "run-bad", "workdir": "ws"},
    )
    assert bad.status_code == 409


def test_preview_does_not_lock(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    created = client.post(
        "/v1/programs",
        headers=headers,
        json={"title": "p", "steps": [_step("s1", 0)]},
    )
    pid = created.json()["program"]["program_id"]
    prev = client.post(f"/v1/programs/{pid}/steps/s1/preview", headers=headers)
    assert prev.status_code == 200
    assert prev.json()["criteria_preview_hash"]
    assert preview_hash(
        Goal.model_validate(prev.json()["goal"])
    ) == prev.json()["criteria_preview_hash"]
    assert app.state.runs == {}


def test_plan_only_envelope(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    created = client.post(
        "/v1/programs",
        headers=headers,
        json={"title": "p", "steps": [_step("s1", 0)]},
    )
    pid = created.json()["program"]["program_id"]
    client.post(f"/v1/programs/{pid}/accept", headers=headers, json={"ack_disclaimer": True})
    env = client.post(
        f"/v1/programs/{pid}/steps/s1/run",
        headers=headers,
        json={"plan_only": True},
    )
    assert env.status_code == 200
    assert "run_create" in env.json()
    assert app.state.runs == {}


def test_suggest_decompositions_and_from_pack(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    sug = client.post(
        "/v1/goals/suggest",
        headers=headers,
        json={
            "context": (
                "Please re-architect the monolith toward hexagonal boundaries and "
                "split the repository interface with backward-compatible adapters. "
                "This is a long migration brief that should prefer a Goal pack."
            ),
            "use_model": False,
        },
    )
    assert sug.status_code == 200, sug.text
    body = sug.json()
    assert body.get("pack")
    assert body.get("decompositions")
    created = client.post(
        "/v1/programs/from-pack",
        headers=headers,
        json={
            "title": "From suggest",
            "intent": body["context"],
            "decomposition": body["decompositions"][0]["decomposition"],
            "steps": body["decompositions"][0]["steps"],
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["program"]["status"] == "draft"
    assert len(created.json()["program"]["steps"]) >= 2


def test_goals_probe_read_only(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app, tenant_id="t1")
    root = tmp_path / "api-root" / "workspaces" / "t1" / "ws"
    root.mkdir(parents=True)
    (root / "README.md").write_text("hi", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    probed = client.post(
        "/v1/goals/probe",
        headers=headers,
        json={"workdir": "ws"},
    )
    assert probed.status_code == 200, probed.text
    assert probed.json()["locked"] is False
    assert probed.json()["probe"]["exists"] is True


def test_skip_step(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    created = client.post(
        "/v1/programs",
        headers=headers,
        json={
            "title": "skip-me",
            "steps": [_step("a", 0), _step("b", 1)],
        },
    )
    pid = created.json()["program"]["program_id"]
    client.post(f"/v1/programs/{pid}/accept", headers=headers, json={"ack_disclaimer": True})
    skipped = client.post(
        f"/v1/programs/{pid}/steps/a/skip",
        headers=headers,
        json={"note": "already characterized offline"},
    )
    assert skipped.status_code == 200, skipped.text
    assert skipped.json()["program"]["steps"][0]["status"] == "skipped"


def _init_git_repo(path: Path, *, filename: str = "README.md", content: str = "v1\n") -> str:
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    (path / filename).write_text(content, encoding="utf-8")
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    # Ensure default branch is main for tip resolution
    subprocess.run(
        ["git", "branch", "-M", "main"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def test_git_tip_unbound_blocked_by_stress_and_accept(tmp_path: Path) -> None:
    from contracts.program import RepoBinding
    from recertia.programs.git_tip import GitTipError, assert_git_tip_program

    step = MigrationStep.model_validate(_step("s1", 0))
    prog = MigrationProgram(
        program_id="p1",
        tenant_id="t1",
        title="t",
        handoff="git_tip",
        steps=[step],
    )
    warnings = stress_step(prog, step)
    assert any(w.code == "missing_repo_binding" and w.severity == "block" for w in warnings)
    with pytest.raises(MaterializeError, match="repo_binding"):
        assert_gp0_execution_prereqs(prog, step, workdir=None, plan_only=False)
    with pytest.raises(GitTipError, match="repo_binding"):
        assert_git_tip_program(prog)

    bound = prog.model_copy(
        update={"repo_binding": RepoBinding(root="app", default_branch="main")}
    )
    assert_git_tip_program(bound)

    client, app = _client(tmp_path)
    headers = _issue(app)
    created = client.post(
        "/v1/programs",
        headers=headers,
        json={
            "title": "unbound tip",
            "handoff": "git_tip",
            "steps": [_step("s1", 0)],
        },
    )
    assert created.status_code == 200
    assert any(
        w["code"] == "missing_repo_binding" for w in created.json()["warnings"]
    )
    pid = created.json()["program"]["program_id"]
    denied = client.post(
        f"/v1/programs/{pid}/accept",
        headers=headers,
        json={"ack_disclaimer": True},
    )
    assert denied.status_code == 400
    assert "repo_binding" in error_text(denied)


def test_git_tip_seed_workdir_and_checkout_failure(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app, tenant_id="t1")
    api_root = tmp_path / "api-root"
    binding_rel = "app"
    repo = api_root / "repo_bindings" / "t1" / binding_rel
    tip_sha = _init_git_repo(repo)

    created = client.post(
        "/v1/programs",
        headers=headers,
        json={
            "title": "tip pack",
            "handoff": "none",
            "steps": [
                _step("char", 0, role="characterization"),
                _step("move", 1, role="structural", path="src"),
            ],
        },
    )
    assert created.status_code == 200, created.text
    pid = created.json()["program"]["program_id"]

    bound = client.post(
        f"/v1/programs/{pid}/repo-binding",
        headers=headers,
        json={"root": binding_rel, "default_branch": "main"},
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()["program"]["handoff"] == "git_tip"
    assert bound.json()["program"]["repo_binding"]["root"] == binding_rel

    accepted = client.post(
        f"/v1/programs/{pid}/accept",
        headers=headers,
        json={"ack_disclaimer": True},
    )
    assert accepted.status_code == 200, accepted.text

    # Record tip on first step from binding root
    recorded = client.post(
        f"/v1/programs/{pid}/steps/char/record-tip",
        headers=headers,
        json={"use_binding_root": True},
    )
    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["head_sha"] == tip_sha
    assert recorded.json()["program"]["steps"][0]["external_handoff"]["head_sha"] == tip_sha

    # Seed second step into a fresh run workdir
    seeded = client.post(
        f"/v1/programs/{pid}/steps/move/seed-workdir",
        headers=headers,
        json={"run_id": "run-seed-1"},
    )
    assert seeded.status_code == 200, seeded.text
    assert seeded.json()["tip_sha"] == tip_sha
    assert seeded.json()["checked_out"] == tip_sha
    dest = Path(seeded.json()["workdir"])
    assert dest.is_dir()
    assert (dest / "README.md").read_text(encoding="utf-8") == "v1\n"
    assert (dest / ".git").exists()

    # Bad tip → step failed / program blocked
    bad = client.post(
        f"/v1/programs/{pid}/steps/move/seed-workdir",
        headers=headers,
        json={"run_id": "run-seed-bad", "tip_sha": "deadbeef" * 5},
    )
    assert bad.status_code == 400
    prog = client.get(f"/v1/programs/{pid}", headers=headers).json()["program"]
    assert prog["status"] == "blocked"
    move = next(s for s in prog["steps"] if s["step_id"] == "move")
    assert move["status"] == "failed"


def test_git_tip_rejects_unregistered_seed(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    created = client.post(
        "/v1/programs",
        headers=headers,
        json={
            "title": "no bind",
            "handoff": "operator_workdir",
            "steps": [_step("s1", 0)],
        },
    )
    pid = created.json()["program"]["program_id"]
    client.post(f"/v1/programs/{pid}/accept", headers=headers, json={"ack_disclaimer": True})
    # Force handoff via store without binding (simulates unbound git_tip)
    prog = app.state.recertia.programs.get(pid, tenant_id="t1")
    assert prog is not None
    app.state.recertia.programs.put(
        prog.model_copy(update={"handoff": "git_tip", "repo_binding": None})
    )
    denied = client.post(
        f"/v1/programs/{pid}/steps/s1/seed-workdir",
        headers=headers,
        json={"run_id": "r1"},
    )
    assert denied.status_code == 400
    assert "unregistered" in error_text(denied) or "repo_binding" in error_text(denied)
    blocked = client.get(f"/v1/programs/{pid}", headers=headers).json()["program"]
    assert blocked["status"] == "blocked"


def test_git_tip_binding_path_escape_rejected(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app, tenant_id="t1")
    created = client.post(
        "/v1/programs",
        headers=headers,
        json={"title": "escape", "steps": [_step("s1", 0)]},
    )
    pid = created.json()["program"]["program_id"]
    denied = client.post(
        f"/v1/programs/{pid}/repo-binding",
        headers=headers,
        json={"root": "../outside"},
    )
    assert denied.status_code == 400
