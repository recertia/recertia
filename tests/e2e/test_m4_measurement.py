"""M4 done-when suite: causal lift harness, regression gate, baselines, eval firewall."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.budget import Budget
from contracts.criteria import (
    SensitivityProof,
    SkillCertificationCriterion,
    TaskCriterion,
    mint_rejecting_proof,
)
from contracts.eval import BinomialSample
from contracts.run import Task
from contracts.skill import FailureMode, Hygiene, Provenance, SkillVersion, Step
from contracts.stats import SkillStats
from contracts.status import SkillStatus
from recertia.evals.statistics import causal_lift
from recertia.evals.store import EvalStore, baseline_from_control
from recertia.graph.engine import GraphOrchestrator
from recertia.memory.episodic import EpisodicStore
from recertia.memory.procedural.promote import PromotionError, promote_to_approved
from recertia.memory.procedural.store import SkillStore
from recertia.memory.semantic import FactStore
from recertia.review.promotion_audit import OperatorAuditStore


def test_synthetic_lift_and_null_are_classified() -> None:
    """Engineering done-when: harness math on injected lift / null (B7)."""

    lift = causal_lift(
        BinomialSample(successes=80, trials=100),
        BinomialSample(successes=50, trials=100),
        task_class="repo-chore",
        snapshot_id="snap-lift",
    )
    assert lift.status == "established_positive"
    assert lift.interval is not None and lift.interval.low > 0

    null = causal_lift(
        BinomialSample(successes=50, trials=100),
        BinomialSample(successes=50, trials=100),
        task_class="repo-chore",
        snapshot_id="snap-null",
    )
    assert null.status == "not_established"
    assert null.render_status() == "not established"


def test_control_baselines_persist_across_snapshots(tmp_path: Path) -> None:
    db = tmp_path / "evals.db"
    store = EvalStore(db)
    a = baseline_from_control(
        task_class="repo-chore",
        snapshot_id="snap-A",
        control=BinomialSample(successes=10, trials=40),
        model_version="m4-test",
        report_id="rA",
    )
    b = baseline_from_control(
        task_class="repo-chore",
        snapshot_id="snap-B",
        control=BinomialSample(successes=12, trials=40),
        model_version="m4-test",
        report_id="rB",
    )
    other = baseline_from_control(
        task_class="other-class",
        snapshot_id="snap-A",
        control=BinomialSample(successes=1, trials=5),
        report_id="rO",
    )
    store.write_baseline(a)
    store.write_baseline(b)
    store.write_baseline(other)
    store.close()

    reopened = EvalStore(db)
    history = reopened.baselines_for("repo-chore")
    assert [h.snapshot_id for h in history] == ["snap-A", "snap-B"]
    assert reopened.latest_baseline("repo-chore").snapshot_id == "snap-B"
    assert reopened.latest_baseline("other-class").snapshot_id == "snap-A"
    assert len(reopened.baselines_for("other-class")) == 1
    reopened.close()


def test_intentionally_bad_skill_blocked_by_regression_gate(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    base = SkillCertificationCriterion(
        id="ok",
        kind="command",
        run="test -f MUST_EXIST.txt",
        preregistered=True,
    )
    bad = SkillVersion(
        skill_id="intentionally-bad",
        version=1,
        title="Intentionally bad skill",
        intent="A skill whose steps cannot satisfy its own golden fixture criterion.",
        task_class="repo-chore",
        steps=[
            Step(
                id="noop",
                tool="shell",
                intent="Do nothing useful so the golden criterion fails",
                inputs={"command": "true"},
            )
        ],
        certification_criteria=[
            base.model_copy(
                update={
                    "sensitivity_proof": mint_rejecting_proof(
                        base, fingerprint="m4-intentionally-bad"
                    )
                }
            )
        ],
        failure_modes=[
            FailureMode(
                symptom="MUST_EXIST.txt is missing after the no-op step",
                response="Write the missing file or refuse promotion of the no-op skill",
            )
        ],
        provenance=Provenance(
            distilled_from_run="synth",
            distilled_at=datetime.now(timezone.utc),
            curation="human_authored",
            authoring_prior_version="ap-test",
        ),
        hygiene=Hygiene(secret_scan="passed", scanned_at=datetime.now(timezone.utc)),
    )
    store.write_version(bad)
    store.write_status(
        SkillStatus(skill_id="intentionally-bad", version=1, lifecycle="candidate", active=False)
    )
    store.write_stats(SkillStats(skill_id="intentionally-bad", version=1))

    golden = tmp_path / "golden" / "repo-chore" / "intentionally-bad"
    (golden / "workspace").mkdir(parents=True)
    (golden / "task.json").write_text(
        '{"request": "create MUST_EXIST.txt", "expected_skill_id": "intentionally-bad"}\n',
        encoding="utf-8",
    )
    (golden / "expect.json").write_text('{"terminal": "solved"}\n', encoding="utf-8")

    with pytest.raises(PromotionError) as exc:
        promote_to_approved(
            store,
            "intentionally-bad",
            1,
            golden_dir=golden,
            runs_root=tmp_path / "runs",
            log_dir=tmp_path / "logs",
            audit=OperatorAuditStore(tmp_path / "operator-audit"),
        )
    # REC-009: decision-only refusal; fixture ids go to the operator audit sink.
    assert exc.value.refusal_kind == "golden_gate"
    assert exc.value.failing_count >= 1
    assert not hasattr(exc.value, "failing_fixtures")
    assert store.get_status("intentionally-bad", 1).lifecycle == "candidate"


def test_eval_firewall_skips_episodic_and_draft(tmp_path: Path) -> None:
    episodic = EpisodicStore(tmp_path / "episodic")
    facts = FactStore(tmp_path / "facts")
    work = tmp_path / "work"
    work.mkdir()
    (work / "output.txt").write_text("x\n", encoding="utf-8")
    orch = GraphOrchestrator(tmp_path / "runs", episodic=episodic, facts=facts)
    try:
        state = orch.start(
            "m4-firewall",
            Task(
                task_id="t",
                request="Ensure output.txt exists",
                task_class="repo-chore",
                submitted_at=datetime.now(timezone.utc),
                is_eval_fixture=True,
            ),
            [
                TaskCriterion(
                    id="gate",
                    kind="command",
                    run="test -f output.txt",
                    source="caller",
                    weight=1.0,
                    sensitivity_proof=SensitivityProof(
                        criterion_id="gate",
                        negative_fixture="empty",
                        rejected=True,
                        checked_at=datetime.now(timezone.utc),
                    ),
                )
            ],
            budget=Budget(max_attempts=1),
            workdir=work,
            script=["true"],
        )
    finally:
        orch.close()

    assert state.terminal == "solved"
    assert state.draft is None
    assert state.facts_extracted == []
    assert state.reusability is not None
    assert "eval firewall" in (state.reusability.reason or "")
    assert episodic.list_index() == []
    assert facts.list_facts() == []
