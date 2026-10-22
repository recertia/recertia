from __future__ import annotations

from datetime import datetime, timezone

from contracts.criteria import SkillCertificationCriterion
from contracts.lint import lint_content_hash
from contracts.skill import Hygiene, Provenance, SkillVersion, Step
from contracts.status import SkillStatus
from recertia.memory.procedural.lint import lint_report
from recertia.validation.sensitivity import author_sensitivity_proof


def _minimal(**overrides) -> SkillVersion:
    now = datetime.now(timezone.utc)
    cert = SkillCertificationCriterion(
        id="ok",
        kind="command",
        run="true",
        preregistered=True,
        sensitivity_proof=author_sensitivity_proof(
            SkillCertificationCriterion(id="ok", kind="command", run="true", preregistered=True)
        ),
    )
    payload = dict(
        skill_id="packaging-demo",
        version=1,
        title="A packaged demo skill here",
        intent="Handle the packaged chore in a generic reusable way for this class.",
        task_class="repo-chore",
        steps=[Step(id="step_1", tool="shell", intent="step_1")],
        certification_criteria=[cert],
        provenance=Provenance(distilled_from_run="r", distilled_at=now),
        hygiene=Hygiene(secret_scan="passed", scanned_at=now),
    )
    payload.update(overrides)
    return SkillVersion(**payload)


def test_packaging_rules_catch_r13_r24() -> None:
    version = _minimal()
    status = SkillStatus(skill_id=version.skill_id, version=1, lifecycle="draft")
    report = lint_report(version, status, skip_if_hash_matches=False)
    codes = {f.code: f.severity for f in report.findings}
    assert codes.get("R1.3") == "warning"
    assert codes.get("R2.4") == "error"


def test_matching_lint_hash_skips_recheck() -> None:
    version = _minimal(
        intent="Apply this only when pyproject.toml exists in the workspace.",
        steps=[Step(id="step_1", tool="shell", intent="Run the packaged command")],
    )
    digest = lint_content_hash(version)
    version = version.model_copy(
        update={"hygiene": version.hygiene.model_copy(update={"lint_content_hash": digest})}
    )
    status = SkillStatus(skill_id=version.skill_id, version=1, lifecycle="draft")
    report = lint_report(version, status, skip_if_hash_matches=True)
    assert report.findings == []
    assert report.content_hash == digest
