from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from contracts.criteria import SkillCertificationCriterion
from contracts.examples import bump_python_dep_stats, bump_python_dep_status, bump_python_dep_version
from contracts.profiles import validate_approved_skill
from contracts.skill import Hygiene, Provenance, SkillVersion, Step
from contracts.stats import ApplyDiversity, SkillStats
from contracts.status import SkillStatus
from recertia.memory.procedural.apply_diversity import note_apply_session
from recertia.memory.procedural.store import SkillStore
from recertia.validation.sensitivity import author_sensitivity_proof


def test_note_apply_session_does_not_rewrite_version(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    version = bump_python_dep_version()
    store.write_version(version)
    store._write_status_unchecked(bump_python_dep_status())
    store.write_stats(SkillStats(skill_id=version.skill_id, version=version.version))
    persisted = store.get_version(version.skill_id, version.version)
    note_apply_session(store, skill_id=version.skill_id, version=version.version, session_id="alice")
    note_apply_session(store, skill_id=version.skill_id, version=version.version, session_id="alice")
    note_apply_session(store, skill_id=version.skill_id, version=version.version, session_id="bob")
    stats = store.get_stats(version.skill_id, version.version)
    assert stats.apply_diversity.distinct_apply_sessions == 2
    # Version bytes unchanged by the stats write.
    assert store.get_version(version.skill_id, version.version) == persisted


def test_single_session_applications_fail_approved_self_distilled_gate() -> None:
    now = datetime.now(timezone.utc)
    criterion = SkillCertificationCriterion(
        id="ok",
        kind="command",
        run="true",
        preregistered=True,
        sensitivity_proof=author_sensitivity_proof(
            SkillCertificationCriterion(id="ok", kind="command", run="true", preregistered=True)
        ),
    )
    version = SkillVersion(
        skill_id="self-distilled-demo",
        version=1,
        title="A reusable self distilled skill",
        intent="Apply the known fix only when the marker file is present.",
        task_class="repo-chore",
        steps=[Step(id="step_1", tool="shell", intent="Run the known fix command")],
        certification_criteria=[criterion],
        provenance=Provenance(
            distilled_from_run="r1",
            distilled_at=now,
            curation="self_distilled",
            derivation="success_transcript",
        ),
        hygiene=Hygiene(secret_scan="passed", scanned_at=now),
    )
    status = SkillStatus(
        skill_id=version.skill_id,
        version=1,
        lifecycle="approved",
        certification=bump_python_dep_status().certification,
    )
    stats = SkillStats(
        skill_id=version.skill_id,
        version=1,
        predictive_trust=bump_python_dep_stats().predictive_trust.model_copy(
            update={"applications": 30, "successes": 28}
        ),
        apply_diversity=ApplyDiversity(distinct_apply_sessions=1, apply_session_sample=["only"]),
    )
    violations = validate_approved_skill(version, status, stats)
    assert any("single application session" in v for v in violations)
