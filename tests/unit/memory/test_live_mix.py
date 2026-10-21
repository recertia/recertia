"""Live-mix admission, predecessor non-regression, and field off-ramp."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.criteria import (
    SkillCertificationCriterion,
    TaskCriterion,
    mint_rejecting_proof,
)
from contracts.run import RunManifest, RunState, SkillCandidateRef, Task
from contracts.skill import FailureMode, Hygiene, Provenance, SkillVersion, Step
from contracts.stats import Contribution, SkillStats
from contracts.status import SkillStatus
from recertia.evals.store import EvalStore
from recertia.jobs.workers import recertify_with_revokes
from recertia.memory.procedural.active_set import assign_active_on_approval, recompute_active_set
from recertia.memory.procedural.live_mix import live_mix_eligible, live_mix_reason
from recertia.memory.procedural.promote import PromotionError, promote_to_approved
from recertia.memory.procedural.seeds import seed_approved_for_tests
from recertia.memory.procedural.store import SkillStore
from recertia.review.autonomy_config import DEFAULT_AUTONOMY
from recertia.review.field_failures import recertify_field_failures
from recertia.review.promotion_audit import OperatorAuditStore

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _proven(cid: str, command: str) -> SkillCertificationCriterion:
    base = SkillCertificationCriterion(
        id=cid, kind="command", run=command, preregistered=True
    )
    return base.model_copy(
        update={"sensitivity_proof": mint_rejecting_proof(base, fingerprint=f"lm-{cid}")}
    )


def _skill(
    skill_id: str,
    *,
    version: int = 1,
    supersedes: int | None = None,
    curation: str = "human_authored",
    command: str = "true",
    criterion_run: str = "true",
) -> SkillVersion:
    return SkillVersion(
        skill_id=skill_id,
        version=version,
        supersedes=supersedes,
        title=f"Title for {skill_id} v{version}",
        intent=f"Intent text long enough for {skill_id} live-mix fixture version.",
        task_class="repo-chore",
        steps=[
            Step(
                id="step_1",
                tool="shell",
                intent="Run the fixture command for this skill version",
                inputs={"command": command},
            )
        ],
        certification_criteria=[_proven("ok", criterion_run)],
        failure_modes=[
            FailureMode(
                symptom="Fixture command exits non-zero on a dirty workspace",
                response="Restore the snapshot and re-run the fixture command",
            )
        ],
        provenance=Provenance(
            distilled_from_run="live-mix",
            distilled_at=_NOW,
            curation=curation,  # type: ignore[arg-type]
            authoring_prior_version="ap-test",
        ),
        hygiene=Hygiene(secret_scan="passed", scanned_at=_NOW),
    )


def _task_criterion(cid: str, command: str) -> dict:
    criterion = TaskCriterion(
        id=cid,
        kind="command",
        run=command,
        source="caller",
        sensitivity_proof=mint_rejecting_proof(
            TaskCriterion(id=cid, kind="command", run=command, source="caller"),
            fingerprint=f"lm-task-{cid}",
        ),
    )
    return criterion.model_dump(mode="json")


def _write_golden(path: Path, *, command: str, workspace_file: str | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "workspace").mkdir(exist_ok=True)
    if workspace_file:
        (path / "workspace" / workspace_file).write_text("x\n", encoding="utf-8")
    (path / "task.json").write_text(
        json.dumps(
            {
                "request": f"satisfy {command}",
                "expected_skill_id": "any",
                "criteria": [_task_criterion("gate", command)],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (path / "expect.json").write_text('{"terminal": "solved"}\n', encoding="utf-8")
    return path


def test_self_distilled_stays_inactive_on_approval() -> None:
    version = _skill("distilled", curation="self_distilled")
    status = SkillStatus(
        skill_id="distilled", version=1, lifecycle="approved", active=False
    )
    stats = SkillStats(skill_id="distilled", version=1)
    assert live_mix_eligible(version, stats) is False
    assigned = assign_active_on_approval(status, version=version, stats=stats)
    assert assigned.active is False
    assert live_mix_reason(version, assigned, stats) == "shadow_trial"


def test_self_distilled_enters_live_mix_when_contribution_non_negative() -> None:
    version = _skill("distilled", curation="self_distilled")
    status = SkillStatus(
        skill_id="distilled", version=1, lifecycle="approved", active=False
    )
    stats = SkillStats(
        skill_id="distilled",
        version=1,
        contribution=Contribution(
            applications=10,
            successes=8,
            suppressed_applications=10,
            suppressed_successes=5,
        ),
    )
    assert live_mix_eligible(version, stats) is True
    assigned = assign_active_on_approval(status, version=version, stats=stats)
    assert assigned.active is True


def test_self_distilled_negative_contribution_stays_off_live_mix() -> None:
    version = _skill("distilled", curation="self_distilled")
    stats = SkillStats(
        skill_id="distilled",
        version=1,
        contribution=Contribution(
            applications=10,
            successes=2,
            suppressed_applications=10,
            suppressed_successes=8,
        ),
    )
    assert live_mix_eligible(version, stats) is False
    status = SkillStatus(
        skill_id="distilled", version=1, lifecycle="approved", active=False
    )
    assert live_mix_reason(version, status, stats) == "negative_contribution"


def test_human_authored_still_goes_active_on_approval() -> None:
    version = _skill("human")
    status = SkillStatus(skill_id="human", version=1, lifecycle="approved", active=False)
    assigned = assign_active_on_approval(
        status, version=version, stats=SkillStats(skill_id="human", version=1)
    )
    assert assigned.active is True


def test_recompute_does_not_activate_ineligible_self_distilled(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    human = _skill("human-live")
    distilled = _skill("distilled-wait", curation="self_distilled")
    seed_approved_for_tests(store, human, active=True)
    seed_approved_for_tests(store, distilled, active=True)
    recompute_active_set(store)
    assert store.get_status("human-live", 1).active is True
    assert store.get_status("distilled-wait", 1).active is False


def test_promote_self_distilled_is_approved_but_not_active(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    version = _skill("distilled-promote", curation="self_distilled")
    store.write_candidate(version)
    golden = _write_golden(tmp_path / "golden" / "distilled-promote", command="true")
    status = promote_to_approved(
        store,
        version.skill_id,
        version.version,
        golden_dir=golden,
        runs_root=tmp_path / "runs",
        log_dir=tmp_path / "logs",
        audit=OperatorAuditStore(tmp_path / "operator-audit"),
    )
    assert status.lifecycle == "approved"
    assert status.active is False


def test_predecessor_non_regression_refuses_successor_that_drops_old_fixture(
    tmp_path: Path,
) -> None:
    store = SkillStore(tmp_path / "skills")
    v1 = _skill(
        "evolving",
        version=1,
        command="python3 -c \"open('MUST_EXIST.txt','w').write('ok')\"",
        criterion_run="test -f MUST_EXIST.txt",
    )
    store.write_candidate(v1)
    fixture_a = _write_golden(
        tmp_path / "golden" / "v1", command="test -f MUST_EXIST.txt"
    )
    logs = tmp_path / "logs"
    first = promote_to_approved(
        store,
        "evolving",
        1,
        golden_dir=fixture_a,
        runs_root=tmp_path / "runs",
        log_dir=logs,
        audit=OperatorAuditStore(tmp_path / "operator-audit"),
    )
    assert first.lifecycle == "approved"
    assert first.active is True

    v2 = _skill(
        "evolving",
        version=2,
        supersedes=1,
        command="true",
        criterion_run="test -f OTHER.txt",
    )
    store.write_candidate(v2)
    fixture_b = _write_golden(
        tmp_path / "golden" / "v2", command="test -f OTHER.txt", workspace_file="OTHER.txt"
    )
    with pytest.raises(PromotionError, match="predecessor non-regression") as exc:
        promote_to_approved(
            store,
            "evolving",
            2,
            golden_dir=fixture_b,
            runs_root=tmp_path / "runs2",
            log_dir=logs,
            audit=OperatorAuditStore(tmp_path / "operator-audit"),
        )
    # REC-009: decision-only refusal; fixture ids go to the operator audit sink.
    assert exc.value.refusal_kind == "predecessor_non_regression"
    assert exc.value.failing_count >= 1
    assert store.get_status("evolving", 2).lifecycle == "candidate"


def _run_state(
    run_id: str,
    *,
    terminal: str,
    skill_id: str = "field-skill",
    arm: str = "treatment",
    eval_fixture: bool = False,
) -> RunState:
    criterion = TaskCriterion(
        id="gate",
        kind="command",
        run="true",
        source="caller",
        sensitivity_proof=mint_rejecting_proof(
            TaskCriterion(id="gate", kind="command", run="true", source="caller"),
            fingerprint="field-gate",
        ),
    )
    return RunState(
        run_id=run_id,
        task=Task(
            task_id=run_id,
            request="do it",
            task_class="repo-chore",
            submitted_at=_NOW,
            is_eval_fixture=eval_fixture,
        ),
        manifest=RunManifest(index_snapshot_id="snap", criteria_hash="locked"),
        arm=arm,  # type: ignore[arg-type]
        criteria=[criterion],
        criteria_locked_at=_NOW,
        chosen=SkillCandidateRef(skill_id=skill_id, version=1, score=1.0),
        attempt_no=1,
        terminal=terminal,  # type: ignore[arg-type]
    )


def test_field_failure_streak_ignores_fixtures_and_other_arms(tmp_path: Path) -> None:
    store = EvalStore(tmp_path / "evals.db")
    store.append_run(_run_state("t1", terminal="unsolved"))
    store.append_run(_run_state("t2", terminal="unsolved"))
    store.append_run(_run_state("fix", terminal="unsolved", eval_fixture=True))
    store.append_run(_run_state("sh", terminal="unsolved", arm="shadow"))
    streaks = store.field_failure_streaks()
    assert streaks[("field-skill", 1)] == 2
    store.close()


def test_solved_breaks_field_failure_streak(tmp_path: Path) -> None:
    store = EvalStore(tmp_path / "evals.db")
    store.append_run(_run_state("old", terminal="unsolved"))
    store.append_run(_run_state("ok", terminal="solved"))
    store.append_run(_run_state("new", terminal="unsolved"))
    assert store.field_failure_streaks()[("field-skill", 1)] == 1
    store.close()


def test_recertifier_quarantines_after_two_field_failures(tmp_path: Path) -> None:
    skills = SkillStore(tmp_path / "skills")
    version = _skill("field-skill")
    seed_approved_for_tests(skills, version, active=True)
    evals = EvalStore(tmp_path / "evals.db")
    evals.append_run(_run_state("a", terminal="unsolved"))
    evals.append_run(_run_state("b", terminal="unsolved"))
    off = recertify_field_failures(skills, evals, config=DEFAULT_AUTONOMY)
    assert off and off[0].skill_id == "field-skill"
    assert skills.get_status("field-skill", 1).lifecycle == "quarantined"
    assert skills.get_status("field-skill", 1).active is False
    evals.close()


def test_recertify_job_wires_field_off_ramp(tmp_path: Path) -> None:
    skills = SkillStore(tmp_path / "skills")
    version = _skill("field-skill")
    seed_approved_for_tests(skills, version, active=True)
    evals = EvalStore(tmp_path / "evals.db")
    evals.append_run(_run_state("a", terminal="unsolved"))
    evals.append_run(_run_state("b", terminal="unsolved"))
    proposals = recertify_with_revokes(skills, eval_store=evals)
    assert any(p.payload.get("reason") == "field_failures" for p in proposals)
    assert skills.get_status("field-skill", 1).lifecycle == "quarantined"
    evals.close()
