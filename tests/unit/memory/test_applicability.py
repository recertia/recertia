from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from contracts.applicability import EnvironmentModel
from contracts.criteria import SkillCertificationCriterion, TaskCriterion
from contracts.skill import FailureMode, Hygiene, Provenance, SkillVersion, Step
from contracts.stats import Contribution, SkillStats
from contracts.status import SkillStatus
from recertia.ledger import HashChainLedger
from recertia.memory.procedural.applicability import check_applicability, refuse_if_inapplicable
from recertia.memory.procedural.store import SkillStore
from recertia.validation.sensitivity import author_sensitivity_proof, empty_negative_fixture


def _cert(run: str = "true") -> SkillCertificationCriterion:
    base = SkillCertificationCriterion(id="ok", kind="command", run=run, preregistered=True)
    return base.model_copy(
        update={
            "sensitivity_proof": author_sensitivity_proof(
                base, negative_workdir=empty_negative_fixture()
            )
        }
    )


def _skill(*, tool: str = "shell", run: str = "true", skill_id: str = "applicable-demo") -> SkillVersion:
    now = datetime.now(timezone.utc)
    return SkillVersion(
        skill_id=skill_id,
        version=1,
        title="Applicability demo skill title",
        intent="Apply this skill when the workspace is a Python project with pyproject.toml.",
        task_class="repo-chore",
        steps=[Step(id="step_1", tool=tool, intent="Run the applicable packaged command")],
        certification_criteria=[_cert(run)],
        failure_modes=[
            FailureMode(
                symptom="Packaged command exits non-zero on a dirty tree",
                response="Restore the snapshot and re-run the packaged command",
            )
        ],
        provenance=Provenance(distilled_from_run="r", distilled_at=now),
        hygiene=Hygiene(secret_scan="passed", scanned_at=now),
    )


def test_unavailable_tool_is_rejected() -> None:
    report = check_applicability(
        _skill(tool="quantum_compiler"),
        environment=EnvironmentModel(tools=["shell", "edit_file"]),
    )
    assert report.ok is False
    assert report.environment_ok is False
    assert any(r.check == "environment" for r in report.reasons)


def test_valid_shell_skill_passes() -> None:
    report = check_applicability(
        _skill(),
        environment=EnvironmentModel(tools=["shell", "edit_file", "read_file"]),
    )
    assert report.ok
    assert report.environment_ok
    assert report.criterion_ok
    assert report.contagion_ok


def test_criterion_mismatch_is_rejected() -> None:
    locked = [
        TaskCriterion(
            id="gate",
            kind="command",
            run="test -f pyproject.toml",
            source="caller",
            weight=1.0,
        )
    ]
    metric = SkillCertificationCriterion(
        id="cost",
        kind="metric",
        metric="cost_usd",
        op="lt",
        threshold=0.01,
        preregistered=True,
        sensitivity_proof=author_sensitivity_proof(
            SkillCertificationCriterion(
                id="cost",
                kind="metric",
                metric="cost_usd",
                op="lt",
                threshold=0.01,
                preregistered=True,
            ),
            negative_workdir=empty_negative_fixture(),
        ),
    )
    version = _skill().model_copy(update={"certification_criteria": [metric]})
    report = check_applicability(version, locked_criteria=locked)
    assert report.ok is False
    assert report.criterion_ok is False


def test_criterion_substring_or_kind_match_is_rejected() -> None:
    locked = [
        TaskCriterion(
            id="gate",
            kind="command",
            run="test -f pyproject.toml",
            source="caller",
            weight=1.0,
        )
    ]
    report = check_applicability(_skill(run="true"), locked_criteria=locked)
    assert report.ok is False
    assert report.criterion_ok is False


def test_criterion_exact_run_match_passes() -> None:
    locked = [
        TaskCriterion(
            id="gate",
            kind="command",
            run="true",
            source="caller",
            weight=1.0,
        )
    ]
    report = check_applicability(_skill(run="true"), locked_criteria=locked)
    assert report.ok
    assert report.criterion_ok


def test_promote_path_without_locked_criteria_requires_non_judge_cert() -> None:
    version = _skill().model_copy(update={"certification_criteria": []})
    report = check_applicability(version)
    assert report.ok is False
    assert report.criterion_ok is False


def test_environment_model_uses_provided_registry() -> None:
    from recertia.memory.procedural.applicability import environment_model_from_registry

    class _Runtime:
        def names(self) -> list[str]:
            return ["edit_file"]

    env = environment_model_from_registry(_Runtime())
    assert env.tools == ["edit_file"]
    report = check_applicability(_skill(tool="shell"), environment=env)
    assert report.environment_ok is False

def test_contagion_near_duplicate_is_rejected(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    rejected = _skill(skill_id="already-rejected")
    store.write_version(rejected)
    store.write_status(
        SkillStatus(skill_id="already-rejected", version=1, lifecycle="quarantined", active=False)
    )
    store.write_stats(
        SkillStats(
            skill_id="already-rejected",
            version=1,
            contribution=Contribution(
                applications=10,
                successes=1,
                suppressed_applications=10,
                suppressed_successes=8,
            ),
        )
    )
    clone = _skill(skill_id="looks-the-same")
    ledger = HashChainLedger(tmp_path / "ledger.jsonl")
    report = refuse_if_inapplicable(clone, store=store, ledger=ledger)
    assert report.ok is False
    assert report.contagion_ok is False
    entries = ledger.entries()
    assert entries
    assert entries[-1].action == "applicability_reject"


def test_contagion_embedding_near_duplicate_is_rejected(tmp_path: Path) -> None:
    """Same title/intent/tools (embedding) but different step intent (hash diverges)."""

    store = SkillStore(tmp_path / "skills")
    rejected = _skill(skill_id="retired-twin")
    store.write_version(rejected)
    store.write_status(
        SkillStatus(skill_id="retired-twin", version=1, lifecycle="benched", active=False)
    )
    store.write_stats(
        SkillStats(
            skill_id="retired-twin",
            version=1,
            contribution=Contribution(
                applications=10,
                successes=0,
                suppressed_applications=10,
                suppressed_successes=8,
            ),
        )
    )
    near = _skill(skill_id="new-twin").model_copy(
        update={
            "steps": [
                _skill().steps[0].model_copy(
                    update={"intent": "Run the applicable packaged command with a dry-run first"}
                )
            ]
        }
    )
    from recertia.memory.procedural.applicability import structural_hash

    assert structural_hash(rejected) != structural_hash(near)
    report = check_applicability(near, store=store)
    assert report.ok is True
    assert report.contagion_ok is True
    assert any("advisory:" in r.message for r in report.reasons if r.check == "contagion")
