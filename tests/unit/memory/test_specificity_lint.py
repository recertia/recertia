from __future__ import annotations

from datetime import datetime, timezone

from contracts.criteria import SkillCertificationCriterion
from contracts.skill import FailureMode, Hygiene, Precondition, Provenance, SkillVersion, Step
from contracts.status import SkillStatus
from recertia.memory.procedural.lint import lint_report
from recertia.validation.sensitivity import author_sensitivity_proof, empty_negative_fixture


def _minimal(**overrides) -> SkillVersion:
    now = datetime.now(timezone.utc)
    cert = SkillCertificationCriterion(
        id="ok",
        kind="command",
        run="true",
        preregistered=True,
        sensitivity_proof=author_sensitivity_proof(
            SkillCertificationCriterion(id="ok", kind="command", run="true", preregistered=True),
            negative_workdir=empty_negative_fixture(),
        ),
    )
    payload = dict(
        skill_id="specificity-demo",
        version=1,
        title="A packaged demo skill here",
        intent="Handle the packaged chore when pyproject.toml exists in the workspace.",
        task_class="repo-chore",
        steps=[Step(id="step_1", tool="shell", intent="Run the packaged command")],
        certification_criteria=[cert],
        preconditions=[
            Precondition(kind="tool_available", value="shell", description="Needs shell")
        ],
        provenance=Provenance(distilled_from_run="r", distilled_at=now),

        hygiene=Hygiene(secret_scan="passed", scanned_at=now),
    )
    payload.update(overrides)
    return SkillVersion(**payload)


def test_draft_without_failure_modes_is_spec_error() -> None:
    version = _minimal()
    status = SkillStatus(skill_id=version.skill_id, version=1, lifecycle="draft")
    report = lint_report(version, status, skip_if_hash_matches=False)
    codes = {f.code: f.severity for f in report.findings}
    assert codes.get("SPEC") == "error"


def test_vague_language_is_flagged_on_draft() -> None:
    version = _minimal(
        intent="Be careful and handle edge cases when the workspace looks messy enough.",
        failure_modes=[
            FailureMode(
                symptom="Install command exits non-zero after a pin bump",
                response="Relax the conflicting pin and re-run the installer",
            )
        ],
    )
    status = SkillStatus(skill_id=version.skill_id, version=1, lifecycle="draft")
    report = lint_report(version, status, skip_if_hash_matches=False)
    codes = {f.code: f.severity for f in report.findings}
    assert codes.get("VAGUE") == "error"


def test_approved_seed_without_failure_modes_is_warning_not_error() -> None:
    version = _minimal()
    status = SkillStatus(skill_id=version.skill_id, version=1, lifecycle="approved")
    report = lint_report(version, status, skip_if_hash_matches=False)
    spec = [f for f in report.findings if f.code == "SPEC"]
    assert spec
    assert all(f.severity == "warning" for f in spec)
    assert not any(f.code == "SPEC" and f.severity == "error" for f in report.findings)


def test_draft_without_preconditions_is_spec_error() -> None:
    version = _minimal(preconditions=[])
    status = SkillStatus(skill_id=version.skill_id, version=1, lifecycle="draft")
    report = lint_report(version, status, skip_if_hash_matches=False)
    messages = [f.message for f in report.findings if f.code == "SPEC"]
    assert any("preconditions required" in m for m in messages)


def test_concrete_failure_mode_passes_draft() -> None:
    version = _minimal(
        failure_modes=[
            FailureMode(
                symptom="pytest.ini is missing after the config write",
                response="Rewrite pytest.ini from the template and re-run pytest --collect-only",
            )
        ]
    )
    status = SkillStatus(skill_id=version.skill_id, version=1, lifecycle="draft")
    report = lint_report(version, status, skip_if_hash_matches=False)
    assert not any(f.code in {"SPEC", "VAGUE"} and f.severity == "error" for f in report.findings)

