"""Regression tests for overnight code-review follow-up findings."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.branch import BranchState
from contracts.budget import Budget
from contracts.criteria import (
    CriterionResult,
    SensitivityProof,
    SkillCertificationCriterion,
    TaskCriterion,
    mint_rejecting_proof,
    sensitivity_evidence_hash,
)
from contracts.run import RunManifest, RunState, Task
from contracts.skill import Hygiene, InputBinding, Provenance, SkillVersion, Step, StepOutput
from contracts.stats import PredictiveTrust, SkillStats
from contracts.status import SkillStatus
from recertia.evals.fake_edges import fake_edge_checks, fake_edge_failure_count, unused_bound_outputs
from recertia.evals.golden import _criteria_from_task
from recertia.graph.ops import OperationLedger
from recertia.jobs.workers import propose_parallelise
from recertia.ledger import HashChainLedger
from recertia.memory.episodic import EpisodicStore
from recertia.memory.procedural.seeds import seed_approved_for_tests
from recertia.memory.procedural.store import SkillStore
from recertia.nodes.context import NodeContext
from recertia.nodes.join import LAYER_THRESHOLD, join
from recertia.nodes.record_dead_end import record_dead_end
from recertia.nodes.validate import score_criteria
from recertia.review.autonomy_config import AutonomyConfig
from recertia.review.shadow import record_shadow_outcome, schedule_shadow_slots
from recertia.workspace import WorkspaceManager

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


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


def _dead_end_state(*, arm: str = "treatment", eval_fixture: bool = False) -> RunState:
    return RunState(
        run_id="r-dead",
        task=Task(
            task_id="r-dead",
            request="fail",
            task_class="repo-chore",
            submitted_at=_NOW,
            is_eval_fixture=eval_fixture,
        ),
        manifest=RunManifest(),
        arm=arm,  # type: ignore[arg-type]
    )


def test_control_arm_dead_end_does_not_write_episodic(tmp_path: Path) -> None:
    episodic = EpisodicStore(tmp_path / "epi")
    record_dead_end(
        _dead_end_state(arm="control"),
        _ctx(tmp_path, node="record_dead_end", episodic=episodic),
    )
    assert episodic.list_index() == []


def test_shadow_arm_dead_end_does_not_write_episodic(tmp_path: Path) -> None:
    episodic = EpisodicStore(tmp_path / "epi")
    record_dead_end(
        _dead_end_state(arm="shadow"),
        _ctx(tmp_path, node="record_dead_end", episodic=episodic),
    )
    assert episodic.list_index() == []


def test_forged_sensitivity_proof_is_not_proven() -> None:
    base = TaskCriterion(id="c1", kind="command", run="true", source="caller")
    forged = base.model_copy(
        update={
            "sensitivity_proof": SensitivityProof(
                criterion_id="c1",
                negative_fixture="empty",
                rejected=True,
                checked_at=_NOW,
                evidence_hash="abc",
            )
        }
    )
    assert not forged.is_preregistered_and_proven


def test_minted_sensitivity_proof_binds_and_gates_validate(tmp_path: Path) -> None:
    base = TaskCriterion(
        id="must-exist",
        kind="command",
        run="test -f missing.txt",
        source="caller",
    )
    proven = base.model_copy(
        update={"sensitivity_proof": mint_rejecting_proof(base, fingerprint="gate-fp")}
    )
    assert proven.is_preregistered_and_proven
    state = RunState(
        run_id="r",
        task=Task(task_id="t", request="x", submitted_at=_NOW),
        criteria=[proven],
    )
    results, failure, notes = score_criteria(state, _ctx(tmp_path, node="validate"))
    assert failure is not None
    assert results[0].passed is False
    assert notes == []


def test_forged_proof_downgrades_to_advisory_in_validate(tmp_path: Path) -> None:
    forged = TaskCriterion(
        id="forged",
        kind="command",
        run="test -f missing.txt",
        source="caller",
        sensitivity_proof=SensitivityProof(
            criterion_id="forged",
            negative_fixture="empty",
            rejected=True,
            checked_at=_NOW,
            checked_against="sha256:nope",
            evidence_hash="deadbeef",
        ),
    )
    assert not forged.is_preregistered_and_proven
    state = RunState(
        run_id="r",
        task=Task(task_id="t", request="x", submitted_at=_NOW),
        criteria=[forged],
    )
    results, failure, notes = score_criteria(state, _ctx(tmp_path, node="validate"))
    assert failure is None
    assert results[0].passed is True
    assert any("advisory" in n for n in notes)


def test_golden_rejects_forged_evidence_hash() -> None:
    base = SkillCertificationCriterion(id="ok", kind="command", run="true", preregistered=True)
    version = SkillVersion(
        skill_id="demo",
        version=1,
        title="Demo skill title long enough",
        intent="Intent text long enough for a golden forgeability unit test.",
        task_class="repo-chore",
        steps=[Step(id="s1", tool="shell", intent="noop step intent text", inputs={"command": "true"})],
        certification_criteria=[
            base.model_copy(
                update={
                    "sensitivity_proof": SensitivityProof(
                        criterion_id="ok",
                        negative_fixture="empty",
                        rejected=True,
                        checked_at=_NOW,
                        evidence_hash="abc",
                    )
                }
            )
        ],
        provenance=Provenance(
            distilled_from_run="t",
            distilled_at=_NOW,
            curation="human_authored",
            authoring_prior_version="ap",
        ),
        hygiene=Hygiene(secret_scan="passed", scanned_at=_NOW),
    )
    with pytest.raises(ValueError, match="hashed rejecting sensitivity"):
        _criteria_from_task(
            {
                "criteria": [
                    {
                        "id": "c1",
                        "kind": "command",
                        "run": "true",
                        "source": "caller",
                        "sensitivity_proof": {
                            "criterion_id": "c1",
                            "negative_fixture": "empty",
                            "rejected": True,
                            "checked_at": "2026-01-01T00:00:00Z",
                            "evidence_hash": "forged",
                        },
                    }
                ]
            },
            version,
        )


def test_layered_portfolio_pruned_low_scores_cannot_win(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, node="join")
    branches: list[BranchState] = []
    for i in range(LAYER_THRESHOLD):
        # Low index = few passes (pruned); high index = many passes (survivors).
        results = [
            CriterionResult(criterion_id=f"c{j}", kind="command", passed=True, weight=1.0)
            for j in range(i)
        ]
        branches.append(
            BranchState(
                branch_id=f"b{i}",
                kind="portfolio",
                strategy="scratch",
                workspace_ref=str(ctx.workdir),
                budget=Budget(),
                status="succeeded",
                results=results,
                cost_usd=0.01,
            )
        )
    # Give the weakest branch an enormous cost advantage that must not override pruning.
    branches[0] = branches[0].model_copy(update={"cost_usd": 0.0})
    state = RunState(
        run_id="r",
        task=Task(task_id="t", request="x", submitted_at=_NOW),
        strategy="portfolio",
        branches=branches,
    )
    outcome = join(state, ctx)
    selected = [b for b in outcome.state.branches if b.selected]
    assert len(selected) == 1
    # Top half by score: b7..b4 survive; b0 (zero passes, cheapest) must not win.
    assert selected[0].branch_id in {f"b{i}" for i in range(4, LAYER_THRESHOLD)}
    assert selected[0].branch_id != "b0"
    assert not any(b.selected and b.branch_id == "b0" for b in outcome.state.branches)


def test_layered_portfolio_all_failed_still_selects_audit_winner(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, node="join")
    branches = [
        BranchState(
            branch_id=f"f{i}",
            kind="portfolio",
            strategy="scratch",
            workspace_ref=str(ctx.workdir),
            budget=Budget(),
            status="failed",
            results=[],
            cost_usd=float(i),
        )
        for i in range(LAYER_THRESHOLD)
    ]
    state = RunState(
        run_id="r-fail",
        task=Task(task_id="t", request="x", submitted_at=_NOW),
        strategy="portfolio",
        branches=branches,
    )
    outcome = join(state, ctx)
    selected = [b for b in outcome.state.branches if b.selected]
    assert len(selected) == 1
    # Deterministic failed audit pick must survive empty layered keep set.
    assert selected[0].branch_id.startswith("f")
    assert "layered" in (outcome.note or "")


def test_candidate_store_adapter_hides_status_mutation(tmp_path: Path) -> None:
    from recertia.memory.procedural.capability import CandidateSkillStoreAdapter

    adapter = CandidateSkillStoreAdapter(SkillStore(tmp_path / "skills"))
    assert not hasattr(adapter, "write_status")
    assert not hasattr(adapter, "write_stats")
    assert not hasattr(adapter, "_write_status_unchecked")
    assert callable(adapter.write_candidate)
    assert callable(adapter.get_version)
    assert callable(adapter.iter_loaded)


def _binding_skill() -> SkillVersion:
    consume = [InputBinding(input="value", source_step="produce", output="value")]
    base = SkillCertificationCriterion(id="ok", kind="command", run="true", preregistered=True)
    return SkillVersion(
        skill_id="bound-demo",
        version=1,
        title="Bound demo skill title",
        intent="Intent long enough for a binding fake-edge unit test skill.",
        task_class="repo-chore",
        steps=[
            Step(
                id="produce",
                tool="shell",
                intent="Produce a typed stdout value for the consumer.",
                outputs=[StepOutput(name="value", type="string")],
            ),
            Step(
                id="consume",
                tool="shell",
                intent="Consume the producer output via an input binding.",
                input_bindings=consume,
            ),
        ],
        certification_criteria=[
            base.model_copy(update={"sensitivity_proof": mint_rejecting_proof(base)})
        ],
        provenance=Provenance(
            distilled_from_run="unit",
            distilled_at=_NOW,
            curation="human_authored",
            derivation="hand_authored",
        ),
        hygiene=Hygiene(secret_scan="passed", scanned_at=_NOW),
    )


def test_partial_transcript_does_not_count_fake_edge_failure() -> None:
    skill = _binding_skill()
    # Only producer started — consumer never ran.
    partial = {
        "events": [
            {"kind": "step_start", "payload": {"step_id": "produce", "input_bindings": []}},
            {
                "kind": "step_output",
                "payload": {
                    "step_id": "produce",
                    "output": "value",
                    "type": "string",
                    "value": "x",
                },
            },
        ]
    }
    assert unused_bound_outputs(skill, partial) == []
    assert fake_edge_checks(skill, partial) == []
    history = [partial for _ in range(5)]
    assert fake_edge_failure_count(skill, history) == 0
    assert not propose_parallelise(
        "bound-demo", 1, skill=skill, transcripts=history, threshold=5
    )


def test_shadow_scheduling_does_not_bump_predictive_trust(tmp_path: Path) -> None:
    from recertia.evals.store import EvalStore

    store = SkillStore(tmp_path / "skills")
    eval_store = EvalStore(tmp_path / "evals.sqlite")
    base = SkillCertificationCriterion(id="ok", kind="command", run="true", preregistered=True)
    version = SkillVersion(
        skill_id="bench-shadow-0",
        version=1,
        title="Shadow bench skill title xx",
        intent="Intent long enough for shadow scheduling predictive trust isolation.",
        task_class="repo-chore",
        steps=[Step(id="s1", tool="shell", intent="noop intent text here", inputs={"command": "true"})],
        certification_criteria=[
            base.model_copy(update={"sensitivity_proof": mint_rejecting_proof(base)})
        ],
        provenance=Provenance(
            distilled_from_run="t",
            distilled_at=_NOW,
            curation="human_authored",
            authoring_prior_version="ap",
        ),
        hygiene=Hygiene(secret_scan="passed", scanned_at=_NOW),
    )
    seed_approved_for_tests(store, version, active=False)
    store.write_status(
        SkillStatus(skill_id=version.skill_id, version=1, lifecycle="benched", active=False)
    )
    store.write_stats(
        SkillStats(
            skill_id=version.skill_id,
            version=1,
            predictive_trust=PredictiveTrust(applications=3, successes=2),
        )
    )
    before = store.get_stats(version.skill_id, 1).predictive_trust
    results = schedule_shadow_slots(
        store,
        eval_store=eval_store,
        config=AutonomyConfig(shadow_slots_per_task_class=1, active_cap_per_task_class=1),
        snapshot_id="shadow-iso",
        evaluate=lambda slot, workdir: True,
    )
    assert results
    after = store.get_stats(version.skill_id, 1).predictive_trust
    assert after.applications == before.applications
    assert after.successes == before.successes
    # Outcome helper alone also must not bump trust.
    record_shadow_outcome(store, version.skill_id, 1, success=True, run_id="x")
    again = store.get_stats(version.skill_id, 1).predictive_trust
    assert again.applications == before.applications
    eval_store.close()


def test_evidence_hash_stable_across_task_and_skill_forms() -> None:
    skill = SkillCertificationCriterion(id="ok", kind="command", run="true", preregistered=True)
    task = TaskCriterion(id="ok", kind="command", run="true", source="caller")
    fp = "same-fp"
    assert sensitivity_evidence_hash(skill, fp) == sensitivity_evidence_hash(task, fp)
    proof = mint_rejecting_proof(skill, fingerprint=fp)
    assert task.model_copy(update={"sensitivity_proof": proof}).is_preregistered_and_proven
