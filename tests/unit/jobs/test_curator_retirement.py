"""Curator applies propose_retirements; the cap pass still does not bench."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from contracts.criteria import (
    CriterionResult,
    SkillCertificationCriterion,
    TaskCriterion,
    mint_rejecting_proof,
)
from contracts.run import RunManifest, RunState, SkillCandidateRef, Task
from contracts.skill import Hygiene, Provenance, SkillVersion, Step
from recertia.evals.store import EvalStore
from recertia.jobs.workers import curator_active_set_and_dedup
from recertia.memory.procedural.seeds import seed_approved_for_tests
from recertia.memory.procedural.store import SkillStore
from recertia.review.autonomy_config import DEFAULT_AUTONOMY


def _skill(skill_id: str) -> SkillVersion:
    base = SkillCertificationCriterion(id="ok", kind="command", run="true", preregistered=True)
    return SkillVersion(
        skill_id=skill_id,
        version=1,
        title=f"Title for {skill_id} skill",
        intent=f"Intent text long enough for {skill_id} curator retirement fixture.",
        task_class="repo-chore",
        steps=[
            Step(
                id="step_1",
                tool="shell",
                intent="Run a trivial shell step for the curator fixture",
                inputs={"command": "true"},
            )
        ],
        certification_criteria=[
            base.model_copy(
                update={"sensitivity_proof": mint_rejecting_proof(base, fingerprint="curator")}
            )
        ],
        provenance=Provenance(
            distilled_from_run="curator",
            distilled_at=datetime.now(timezone.utc),
            curation="human_authored",
            authoring_prior_version="ap-test",
        ),
        hygiene=Hygiene(secret_scan="passed", scanned_at=datetime.now(timezone.utc)),
    )


def _record_evidence(
    eval_store: EvalStore,
    *,
    skill_id: str,
    prefix: str,
    shadow: tuple[int, int],
    suppression: tuple[int, int],
) -> None:
    criterion = TaskCriterion(id="non-judge", kind="command", run="true", source="caller")

    def append(run_id: str, arm: str, success: bool, chosen: bool) -> None:
        eval_store.append_run(
            RunState(
                run_id=run_id,
                task=Task(
                    task_id=run_id,
                    request="evidence fixture",
                    task_class="repo-chore",
                    submitted_at=datetime.now(timezone.utc),
                ),
                manifest=RunManifest(index_snapshot_id="evidence-snapshot", criteria_hash="locked"),
                arm=arm,  # type: ignore[arg-type]
                criteria=[criterion],
                criteria_locked_at=datetime.now(timezone.utc),
                chosen=(
                    SkillCandidateRef(skill_id=skill_id, version=1, score=1.0) if chosen else None
                ),
                suppressed_skill=(
                    SkillCandidateRef(skill_id=skill_id, version=1, score=1.0)
                    if arm == "control"
                    else None
                ),
                attempt_no=1,
                results=[
                    CriterionResult(criterion_id=criterion.id, kind="command", passed=success)
                ],
                terminal="solved" if success else "unsolved",
            )
        )

    successes, trials = shadow
    for index in range(trials):
        append(f"{prefix}-s-{index}", "shadow", index < successes, True)
    successes, trials = suppression
    for index in range(trials):
        append(f"{prefix}-c-{index}", "control", index < successes, False)


def test_curator_without_eval_store_does_not_bench(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    seed_approved_for_tests(store, _skill("healthy"), active=True)
    proposals = curator_active_set_and_dedup(store)
    assert store.get_status("healthy", 1).lifecycle == "approved"
    assert all(not p.payload.get("retirement") for p in proposals)


def test_curator_benches_negative_contribution_past_floor(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    eval_store = EvalStore(tmp_path / "evals.sqlite")
    seed_approved_for_tests(store, _skill("neg-contrib"), active=True)
    floor = DEFAULT_AUTONOMY.evidence_floor
    _record_evidence(
        eval_store,
        skill_id="neg-contrib",
        prefix="neg",
        shadow=(5, floor),
        suppression=(floor - 5, floor),
    )
    proposals = curator_active_set_and_dedup(store, eval_store=eval_store)
    eval_store.close()
    assert store.get_status("neg-contrib", 1).lifecycle == "benched"
    assert any(p.payload.get("retirement") for p in proposals)


def test_curator_does_not_bench_below_floor(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    eval_store = EvalStore(tmp_path / "evals.sqlite")
    seed_approved_for_tests(store, _skill("thin"), active=True)
    _record_evidence(
        eval_store,
        skill_id="thin",
        prefix="thin",
        shadow=(0, 5),
        suppression=(4, 5),
    )
    curator_active_set_and_dedup(store, eval_store=eval_store)
    eval_store.close()
    assert store.get_status("thin", 1).lifecycle == "approved"


def test_curator_flags_low_specificity_active_skills(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    seed_approved_for_tests(store, _skill("vague-seed"), active=True)
    from recertia.ledger import HashChainLedger

    ledger = HashChainLedger(tmp_path / "ledger.jsonl")
    proposals = curator_active_set_and_dedup(store, ledger=ledger)
    flagged = [p for p in proposals if p.payload.get("specificity")]
    assert flagged
    assert flagged[0].skill_id == "vague-seed"
    assert "SPEC" in flagged[0].payload["codes"]
    assert not any(entry.action == "lint_reject" for entry in ledger.entries())
    second = curator_active_set_and_dedup(store, ledger=ledger, existing_proposals=proposals)
    assert not [p for p in second if p.payload.get("specificity")]
    assert not any(entry.action == "lint_reject" for entry in ledger.entries())


def test_curator_persists_specificity_across_process_restarts(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    seed_approved_for_tests(store, _skill("vague-seed"), active=True)
    log = tmp_path / "proposals.jsonl"
    first = curator_active_set_and_dedup(store, proposals_path=log)
    assert any(p.payload.get("specificity") for p in first)
    assert log.is_file()
    second = curator_active_set_and_dedup(store, proposals_path=log)
    assert not [p for p in second if p.payload.get("specificity")]
