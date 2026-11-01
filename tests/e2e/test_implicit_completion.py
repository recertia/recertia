"""Implicit backlog completion: container, store, API, scope, layered join, practice, domain-2."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import pytest

from contracts.branch import BranchState
from contracts.budget import Budget
from contracts.criteria import SensitivityProof, SkillCertificationCriterion
from contracts.fact import Fact, FactProvenance
from contracts.run import RunState, Task
from contracts.skill import FailureMode, Hygiene, Provenance, SkillVersion, Step
from recertia.api import create_app
from recertia.evals.second_domain import research_synthesis_lift, second_domain_fixture_ready
from recertia.graph.ops import OperationLedger
from recertia.jobs import JobRunner
from recertia.jobs.workers import (
    draft_from_mine_proposal,
    mine_from_repo_hints,
    practice_from_one_offs,
)
from recertia.ledger import HashChainLedger
from recertia.memory.procedural.promote import promote_to_approved
from recertia.memory.procedural.seeds import seed_approved_for_tests
from recertia.memory.procedural.store import SkillStore
from recertia.memory.scope import (
    promote_fact_scope,
    promote_skill_scope,
    tenant_readable,
)
from recertia.memory.semantic import FactStore
from recertia.nodes.context import NodeContext
from recertia.nodes.join import LAYER_THRESHOLD, join
from recertia.review.promotion_audit import OperatorAuditStore
from recertia.solver.container import run_with_backend
from recertia.store.backend import open_backend, postgres_dialect_mentions_pgvector
from recertia.store.blobs import FilesystemBlobStore
from recertia.store.vectors import JsonBlobVectorIndex, open_vector_index
from recertia.telemetry import (
    JsonlSpanExporter,
    reset_telemetry,
    write_dashboard,
)
from recertia.workspace import WorkspaceManager


def _ctx(tmp_path: Path, node: str = "join") -> NodeContext:
    work = tmp_path / "w"
    work.mkdir(exist_ok=True)
    return NodeContext(
        run_id="r",
        attempt_no=1,
        node=node,
        workdir=work,
        workspaces=WorkspaceManager(tmp_path / "snap"),
        ledger=HashChainLedger(tmp_path / "l.jsonl"),
        ops=OperationLedger(tmp_path / "o.db"),
    )


def _skill(skill_id: str, *, scope: str = "project") -> SkillVersion:
    proof = SensitivityProof(
        criterion_id="ok",
        negative_fixture="empty",
        rejected=True,
        checked_at=datetime.now(timezone.utc),
    )
    return SkillVersion(
        skill_id=skill_id,
        version=1,
        title=f"Title for {skill_id} skill",
        intent=f"Intent text long enough for {skill_id} skill version contract.",
        task_class="repo-chore",
        scope=scope,  # type: ignore[arg-type]
        steps=[
            Step(
                id="step_1",
                tool="shell",
                intent="Run a trivial shell step for the fixture",
                inputs={"command": "true"},
            )
        ],
        certification_criteria=[
            SkillCertificationCriterion(
                id="ok",
                kind="command",
                run="true",
                sensitivity_proof=proof,
                preregistered=True,
            )
        ],
        failure_modes=[
            FailureMode(
                symptom="Trivial shell step exits non-zero in the fixture workspace",
                response="Restore the snapshot and re-run the shell step",
            )
        ],
        provenance=Provenance(
            distilled_from_run="t",
            distilled_at=datetime.now(timezone.utc),
            curation="human_authored",
            authoring_prior_version="ap",
        ),
        hygiene=Hygiene(secret_scan="passed", scanned_at=datetime.now(timezone.utc)),
    )


def test_container_backend_fails_closed_without_runtime(tmp_path: Path, monkeypatch) -> None:
    work = tmp_path / "jail"
    work.mkdir()
    monkeypatch.setattr("recertia.solver.container.container_runtime", lambda: None)
    with pytest.raises(Exception, match="container runtime"):
        run_with_backend("true", workdir=work)


def test_sqlite_backend_and_pgvector_dialect(tmp_path: Path) -> None:
    backend = open_backend(sqlite_path=tmp_path / "store.sqlite")
    assert backend.dialect == "sqlite"
    assert "001_init" in backend.apply_migrations()
    assert "embeddings" in backend.table_names()
    assert postgres_dialect_mentions_pgvector()


def test_vector_index_and_blob_store(tmp_path: Path) -> None:
    idx = open_vector_index(tmp_path / "vec.sqlite")
    try:
        idx.upsert("a", "add pytest config to the project")
        idx.upsert("b", "draft a structured research brief")
        hits = idx.search("pytest config")
        assert hits and hits[0][0] == "a"
        assert idx.backend_name() in ("json-blob", "sqlite-vec")
    finally:
        cast(JsonBlobVectorIndex, idx).close()
    blobs = FilesystemBlobStore(tmp_path / "blobs")
    digest = blobs.put(b"hello transcript")
    assert blobs.exists(digest)
    assert blobs.get(digest) == b"hello transcript"


def test_otel_jsonl_export_and_dashboard(tmp_path: Path) -> None:
    tel = reset_telemetry(admin_actor="test-admin", tenant_id="test")
    exporter = JsonlSpanExporter(tmp_path / "spans.jsonl")
    tel.add_exporter(exporter)
    with tel.span("run", tenant_id="test", run_id="r1"):
        for name in (
            "run.started",
            "run.finished",
            "node.started",
            "node.finished",
            "tool.invoked",
            "judge.context.opened",
            "merge.audited",
            "policy.changed",
            "scope.promoted",
        ):
            tel.emit(name, tenant_id="test", run_id="r1")
    assert tel.missing_required() == []
    assert (tmp_path / "spans.jsonl").read_text().strip()
    dash = write_dashboard(tel, tmp_path / "dashboard.json", tenant_id="test")
    assert "required_events" in dash.read_text()


def test_skill_scope_promotion_and_tenant_isolation(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    facts = FactStore(tmp_path / "facts")
    ledger = HashChainLedger(tmp_path / "ledger.jsonl")
    v = _skill("scoped-skill", scope="project")
    seed_approved_for_tests(store, v, active=True)
    stamped, rec = promote_skill_scope(store, v, to_scope="org", reviewer="alice", ledger=ledger)
    assert stamped.scope == "org"
    assert stamped.version == 2
    assert rec.artifact_kind == "skill"
    fact = Fact(
        fact_id="tenant-a-note",
        scope="project",
        slug="tenant-a-note",
        assertion="Project A only note without secrets.",
        provenance=FactProvenance(asserting_human="a"),
        authored_at=datetime.now(timezone.utc),
    )
    facts.write(fact)
    promote_fact_scope(facts, fact, to_scope="org", reviewer="alice")
    assert tenant_readable("org", {"org", "global"})
    assert not tenant_readable("org", {"project"})
    # Project-scoped caller must not see org-only artifact scope.
    assert not tenant_readable("org", {"run", "project"})


def test_fastapi_health_runs_blobs_dashboard(tmp_path: Path, monkeypatch) -> None:
    fastapi = pytest.importorskip("fastapi")
    _ = fastapi
    from fastapi.testclient import TestClient

    app = create_app(root=tmp_path / "api-root")
    issued = app.state.api_keys.issue(
        tenant_id="test", scopes={"runs", "exec", "blobs", "metrics"}, actor="test-admin"
    )
    client = TestClient(app)
    headers = {"X-API-Key": issued.secret}
    assert client.get("/health").json()["status"] == "ok"
    created = client.post(
        "/v1/runs",
        json={
            "request": "do chore",
            "task_class": "repo-chore",
            "budget": {"max_attempts": 1},
            "script": ["true"],
        },
        headers=headers,
    )
    assert created.status_code == 200
    payload = created.json()
    run_id = payload["run_id"]
    assert payload.get("terminal") is not None
    assert client.get(f"/v1/runs/{run_id}", headers=headers).status_code == 200
    put = client.post(
        "/v1/blobs", json={"data": "snap", "content_type": "text/plain"}, headers=headers
    )
    digest = put.json()["digest"]
    assert client.get(f"/v1/blobs/{digest.removeprefix('sha256:')}", headers=headers).status_code == 200
    dash = client.get("/v1/metrics/dashboard", headers=headers).json()
    assert "panels" in dash


def test_layered_fan_in_when_branch_count_high(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    branches = [
        BranchState(
            branch_id=f"b{i}",
            kind="portfolio",
            strategy="scratch",
            workspace_ref=f"w{i}",
            budget=Budget(),
            status="succeeded",
            cost_usd=0.01 * (i + 1),
        )
        for i in range(LAYER_THRESHOLD)
    ]
    state = RunState(
        run_id="r",
        task=Task(task_id="t", request="x", submitted_at=datetime.now(timezone.utc)),
        strategy="portfolio",
        branches=branches,
    )
    outcome = join(state, ctx)
    assert outcome.state.merge_audits[-1].layered is True
    assert any(b.selected for b in outcome.state.branches)


def test_practice_curriculum_and_miner_then_golden_promote(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    curriculum = tmp_path / "curriculum"
    props = practice_from_one_offs(
        ["one-off: missing pytest.ini cluster"], curriculum_dir=curriculum
    )
    assert props and (curriculum / "practice-0.json").exists()
    assert props[0].payload["predicted_success_band"] == [0.2, 0.8]

    mined = mine_from_repo_hints(store, hints=["Add CI workflow from HISTORY"])
    draft = draft_from_mine_proposal(mined[0])
    runner = JobRunner(store, golden_root=None)
    result = runner.submit_proposal(mined[0], draft)
    assert result.startswith("candidate:")
    assert store.get_status(draft.skill_id, 1).lifecycle == "candidate"

    # External golden-gate path: fixture for this mined skill, then promote succeeds.
    golden_dir = tmp_path / "golden" / draft.skill_id
    (golden_dir / "workspace").mkdir(parents=True)
    (golden_dir / "workspace" / "ok.txt").write_text("x\n", encoding="utf-8")
    (golden_dir / "task.json").write_text(
        '{"request":"noop","task_class":"repo-chore"}\n', encoding="utf-8"
    )
    (golden_dir / "expect.json").write_text('{"script":["true"]}\n', encoding="utf-8")
    status = promote_to_approved(
        store,
        draft.skill_id,
        draft.version,
        golden_dir=golden_dir,
        runs_root=tmp_path / "runs",
        log_dir=tmp_path / "logs",
        require_task_class_gate=False,
        audit=OperatorAuditStore(tmp_path / "operator-audit"),
    )
    assert status.lifecycle == "approved"
    assert status.active is True


def test_second_domain_lift_reports_not_established() -> None:
    assert second_domain_fixture_ready()
    # Balanced small samples → interval includes zero → not_established
    result = research_synthesis_lift(
        treatment_successes=5,
        treatment_trials=10,
        control_successes=5,
        control_trials=10,
    )
    assert result.task_class == "research-synthesis"
    assert result.status == "not_established"
    # Sanity: positive injection establishes
    pos = research_synthesis_lift(
        treatment_successes=90,
        treatment_trials=100,
        control_successes=10,
        control_trials=100,
    )
    assert pos.status == "established_positive"
