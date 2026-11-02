"""Regression tests for the overnight code-review hardening pass."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.criteria import SkillCertificationCriterion, TaskCriterion, mint_rejecting_proof
from contracts.run import RunManifest, RunState, Task
from contracts.skill import Hygiene, Provenance, SkillVersion, Step
from contracts.stats import Contribution, PredictiveTrust, SkillStats
from recertia.api.auth import ApiKeyStore
from recertia.evals.golden import _criteria_from_task
from recertia.evals.metrics import build_metric_report
from recertia.graph.ops import OperationLedger
from recertia.ledger import HashChainLedger
from recertia.memory.episodic import EpisodicStore
from recertia.memory.procedural.active_set import recompute_active_set
from recertia.memory.procedural.allocate import allocate_and_write, allocate_next_version
from recertia.memory.procedural.seeds import seed_approved_for_tests
from recertia.memory.procedural.store import SkillStore
from recertia.nodes.context import NodeContext
from recertia.nodes.distill import distill
from recertia.nodes.record_dead_end import record_dead_end
from recertia.review.autonomy_config import AutonomyConfig
from recertia.solver.container import ContainerSpec, run_in_container
from recertia.solver.model import StubModelClient
from recertia.solver.sandbox import SandboxError
from recertia.solver.tools import ToolRuntime, default_registry
from recertia.telemetry import Telemetry, render_dashboard
from recertia.validation.assertions import UnsafeAssertionError, evaluate_assertion
from recertia.validation.sensitivity import author_sensitivity_proof
from recertia.workspace import WorkspaceManager
from tests.support.symlinks import require_symlink_support

_OPEN_LEDGERS: list[OperationLedger] = []


@pytest.fixture(autouse=True)
def _close_operation_ledgers() -> Iterator[None]:
    """Close every operation ledger ``_ctx`` opened during the test."""

    yield
    while _OPEN_LEDGERS:
        _OPEN_LEDGERS.pop().close()


def _ctx(tmp_path: Path, *, node: str, episodic: EpisodicStore | None = None) -> NodeContext:
    workdir = tmp_path / "workdir"
    workdir.mkdir(exist_ok=True)
    ops = OperationLedger(tmp_path / "ops.db")
    _OPEN_LEDGERS.append(ops)
    return NodeContext(
        run_id="review-run",
        attempt_no=0,
        node=node,
        workdir=workdir,
        workspaces=WorkspaceManager(tmp_path / "snapshots"),
        ledger=HashChainLedger(tmp_path / "ledger.jsonl"),
        ops=ops,
        episodic=episodic,
    )


def _version(skill_id: str) -> SkillVersion:
    base = SkillCertificationCriterion(
        id="ok",
        kind="command",
        run="true",
        preregistered=True,
    )
    return SkillVersion(
        skill_id=skill_id,
        version=1,
        title=f"Title for {skill_id} skill",
        intent=f"Intent text long enough for {skill_id} skill version contract.",
        task_class="repo-chore",
        steps=[
            Step(
                id="step_1",
                tool="shell",
                intent="Run a trivial shell step for the hardening fixture",
                inputs={"command": "true"},
            )
        ],
        certification_criteria=[
            base.model_copy(
                update={
                    "sensitivity_proof": mint_rejecting_proof(base, fingerprint="review-ok")
                }
            )
        ],
        provenance=Provenance(
            distilled_from_run="review",
            distilled_at=datetime.now(timezone.utc),
            curation="human_authored",
            authoring_prior_version="ap-test",
        ),
        hygiene=Hygiene(secret_scan="passed", scanned_at=datetime.now(timezone.utc)),
    )


def test_assertion_cannot_escape_workdir(tmp_path: Path) -> None:
    (tmp_path / "ok.txt").write_text("hi")
    assert evaluate_assertion("(workdir / 'ok.txt').read_text() == 'hi'", workdir=tmp_path)
    with pytest.raises(UnsafeAssertionError):
        evaluate_assertion("Path('/tmp/pwn').write_text('x')", workdir=tmp_path)
    with pytest.raises(UnsafeAssertionError):
        evaluate_assertion("(workdir / '..' / 'etc' / 'passwd').read_text()", workdir=tmp_path)


def test_bare_path_method_attribute_is_not_truthy(tmp_path: Path) -> None:
    """``.exists`` without ``()`` must not evaluate as a truthy bound method."""

    missing = tmp_path / "nope.txt"
    assert not missing.exists()
    with pytest.raises(UnsafeAssertionError, match="must be called"):
        evaluate_assertion("(workdir / 'nope.txt').exists", workdir=tmp_path)
    with pytest.raises(UnsafeAssertionError, match="must be called"):
        evaluate_assertion("(workdir / 'nope.txt').is_file", workdir=tmp_path)
    assert evaluate_assertion("not (workdir / 'nope.txt').exists()", workdir=tmp_path)
    (tmp_path / "ok.txt").write_text("x")
    assert evaluate_assertion("(workdir / 'ok.txt').exists()", workdir=tmp_path)
    # Non-callable path properties remain usable without a call.
    assert evaluate_assertion("(workdir / 'ok.txt').name == 'ok.txt'", workdir=tmp_path)


def test_grep_skips_symlinks_outside_workspace(tmp_path: Path) -> None:
    require_symlink_support(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("token-outside")
    (work / "leak.txt").symlink_to(secret)
    (work / "local.txt").write_text("token-inside")

    tools = ToolRuntime(default_registry(), require_approval_for_non_read=True)
    result = tools.invoke("grep", {"pattern": "token", "path": "."}, workdir=work, step_id="s1")
    assert "token-inside" in result.stdout
    assert "token-outside" not in result.stdout


def test_container_spec_policy_rejects_host_network_and_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("recertia.solver.container.container_runtime", lambda: "docker")
    with pytest.raises(SandboxError, match="network"):
        run_in_container("true", workdir=tmp_path, spec=ContainerSpec(network="host"))
    with pytest.raises(SandboxError, match="root"):
        run_in_container("true", workdir=tmp_path, spec=ContainerSpec(user="0:0"))
    with pytest.raises(SandboxError, match="writable"):
        run_in_container("true", workdir=tmp_path, spec=ContainerSpec(read_only_root=False))


def test_container_run_drops_capabilities_and_blocks_privilege_escalation(
    tmp_path: Path, monkeypatch
) -> None:
    captured: list[list[str]] = []

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(list(args))
        from subprocess import CompletedProcess

        return CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("recertia.solver.container.container_runtime", lambda: "docker")
    monkeypatch.setattr("recertia.solver.container.subprocess.run", fake_run)
    run_in_container("true", workdir=tmp_path)
    assert captured
    args = captured[0]
    assert "--cap-drop=ALL" in args
    assert args[args.index("--security-opt") + 1] == "no-new-privileges"


def test_api_key_issue_rejects_path_tenant_ids(tmp_path: Path) -> None:
    keys = ApiKeyStore(tmp_path / "keys.sqlite")
    with pytest.raises(ValueError, match="tenant_id"):
        keys.issue(tenant_id="../escape", scopes={"runs"}, actor="op")
    with pytest.raises(ValueError, match="unknown scopes"):
        keys.issue(tenant_id="tenant-a", scopes={"runs", "shell"}, actor="op")


def test_verifier_identity_rejects_matching_model_metadata() -> None:
    solver = StubModelClient(provider="openai", model_id="gpt-test", role="solver")
    verifier = StubModelClient(provider="openai", model_id="gpt-test", role="verifier")
    assert solver.shares_identity_with(verifier)
    other = StubModelClient(provider="openai", model_id="gpt-judge", role="verifier")
    assert not solver.shares_identity_with(other)


def test_control_arm_does_not_write_episodic(tmp_path: Path) -> None:
    episodic = EpisodicStore(tmp_path / "epi")
    state = RunState(
        run_id="r-control",
        task=Task(
            task_id="r-control",
            request="do thing",
            task_class="repo-chore",
            submitted_at=datetime.now(timezone.utc),
        ),
        manifest=RunManifest(),
        arm="control",
    )
    distill(state, _ctx(tmp_path, node="distill", episodic=episodic))
    assert episodic.list_index() == []


def test_shadow_arm_does_not_write_episodic_or_draft(tmp_path: Path) -> None:
    episodic = EpisodicStore(tmp_path / "epi")
    state = RunState(
        run_id="r-shadow",
        task=Task(
            task_id="r-shadow",
            request="do thing",
            task_class="repo-chore",
            submitted_at=datetime.now(timezone.utc),
        ),
        manifest=RunManifest(),
        arm="shadow",
        terminal="solved",
    )
    outcome = distill(state, _ctx(tmp_path, node="distill", episodic=episodic))
    assert episodic.list_index() == []
    assert outcome.state.draft is None
    assert outcome.route == "one_off"
    assert "shadow" in (outcome.note or "")


def test_eval_fixture_dead_end_does_not_write_episodic(tmp_path: Path) -> None:
    episodic = EpisodicStore(tmp_path / "epi")
    state = RunState(
        run_id="r-fix",
        task=Task(
            task_id="r-fix",
            request="fixture",
            task_class="repo-chore",
            submitted_at=datetime.now(timezone.utc),
            is_eval_fixture=True,
        ),
        manifest=RunManifest(),
    )
    record_dead_end(state, _ctx(tmp_path, node="record_dead_end", episodic=episodic))
    assert episodic.list_index() == []


def test_active_set_without_eval_store_keeps_top_approved(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    for i in range(4):
        sid = f"keep-{i}"
        version = _version(sid)
        seed_approved_for_tests(store, version, active=True)
        store.write_stats(
            SkillStats(
                skill_id=sid,
                version=1,
                predictive_trust=PredictiveTrust(applications=10 + i, successes=5 + i),
                contribution=Contribution(
                    applications=10 + i,
                    successes=5 + i,
                    suppressed_applications=10,
                    suppressed_successes=3,
                ),
            )
        )
    recompute_active_set(store, config=AutonomyConfig(active_cap_per_task_class=2))
    actives = [
        status
        for _v, status, _st in store.iter_loaded()
        if status.lifecycle == "approved" and status.active
    ]
    assert len(actives) == 2


def test_golden_task_criteria_require_sensitivity_proof() -> None:
    version = _version("demo")
    with pytest.raises(ValueError, match="hashed rejecting sensitivity"):
        _criteria_from_task(
            {
                "criteria": [
                    {"id": "c1", "kind": "assertion", "expr": "True", "source": "caller"}
                ]
            },
            version,
        )


def test_shadow_observations_excluded_from_user_metrics() -> None:
    rows = [
        {
            "is_eval_fixture": False,
            "arm": "shadow",
            "first_attempt_success": True,
            "strategy": "apply",
            "terminal": "solved",
            "attempt_no": 1,
            "cost_usd": 1.0,
        },
        {
            "is_eval_fixture": False,
            "arm": "treatment",
            "first_attempt_success": False,
            "strategy": "scratch",
            "terminal": "unsolved",
            "attempt_no": 1,
        },
    ]
    report = build_metric_report(rows, snapshot_id="snap")
    assert report.first_attempt_success == 0.0


def test_telemetry_missing_required_is_tenant_scoped() -> None:
    tel = Telemetry()
    for name in (
        "run.started",
        "retrieve.completed",
        "solve.completed",
        "validate.completed",
        "review.decision",
        "run.finished",
    ):
        tel.emit(name, tenant_id="tenant-b", run_id="r-b")
    dash = render_dashboard(tel, tenant_id="tenant-a")
    assert dash["missing_required"]
    assert "run.started" in dash["missing_required"]


def test_allocate_and_write_honours_reservations(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    reserved = allocate_next_version(store, "alloc-demo")
    assert reserved == 1
    stamped = allocate_and_write(store, _version("alloc-demo"))
    assert stamped.version == 2


def test_sensitivity_default_runner_uses_restricted_assertions(tmp_path: Path) -> None:
    criterion = TaskCriterion(id="c1", kind="assertion", expr="True", source="caller")
    proof = author_sensitivity_proof(criterion, negative_workdir=tmp_path)
    assert proof.rejected is False
    bad = TaskCriterion(
        id="c2",
        kind="assertion",
        expr="Path('/tmp/x').write_text('no')",
        source="caller",
    )
    proof_bad = author_sensitivity_proof(bad, negative_workdir=tmp_path)
    # Unsafe expressions are treated as failed (rejected=True) rather than executing.
    assert proof_bad.rejected is True
