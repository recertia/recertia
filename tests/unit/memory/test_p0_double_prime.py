from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from contracts.examples import bump_python_dep_status, bump_python_dep_version
from contracts.lint import lint_content_hash
from contracts.policy import Policy
from contracts.skill import Hygiene, Provenance, SkillVersion, Step
from contracts.status import SkillStatus
from recertia.graph.engine import GraphOrchestrator
from recertia.jobs import Proposal
from recertia.jobs.workers import draft_from_mine_proposal
from recertia.memory.procedural.lineage import LineageIndex, LineageServices
from recertia.memory.procedural.lint import lint_report
from recertia.memory.procedural.store import SkillStore
from recertia.review.lifecycle import quarantine_on_failures


def _version_with_source(run_id: str = "poison-run") -> SkillVersion:
    version = bump_python_dep_version()
    return version.model_copy(
        update={
            "provenance": version.provenance.model_copy(update={"source_run_ids": [run_id]})
        }
    )


def test_write_candidate_stamps_lint_hash(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    version = bump_python_dep_version()
    written = store.write_candidate(version)
    digest = lint_content_hash(version)
    assert written.hygiene.lint_content_hash == digest
    on_disk = store.get_version(version.skill_id, version.version)
    assert on_disk.hygiene.lint_content_hash == digest
    report = lint_report(
        on_disk,
        store.get_status(version.skill_id, version.version),
        skip_if_hash_matches=True,
    )
    assert report.findings == []


def test_write_version_records_lineage(tmp_path: Path) -> None:
    index = LineageIndex(tmp_path / "lineage.jsonl")
    store = SkillStore(tmp_path / "skills", lineage_index=index)
    version = _version_with_source("r-1")
    store.write_version(version)
    assert (version.skill_id, version.version) in index.lookup("run", "r-1")
    # Second record of the same version (via rebuild) does not duplicate.
    index.record(version)
    hits = index.lookup("run", "r-1")
    assert hits.count((version.skill_id, version.version)) == 1


def test_lineage_lookup_is_point_get(tmp_path: Path) -> None:
    index = LineageIndex(tmp_path / "lineage.jsonl")
    store = SkillStore(tmp_path / "skills", lineage_index=index)
    version = _version_with_source("r-point")
    store.write_version(version)
    assert index.idx_path.exists()
    # Drop WAL; lookup must still hit the idx.
    index.path.write_text("", encoding="utf-8")
    index._map = None
    assert index.lookup("run", "r-point") == [(version.skill_id, version.version)]
    rebuilt = LineageIndex(tmp_path / "other.jsonl")
    n = rebuilt.rebuild(store)
    assert n == 1
    assert rebuilt.lookup("run", "r-point") == [(version.skill_id, version.version)]


def test_quarantine_enqueues_revoke(tmp_path: Path) -> None:
    services = LineageServices.open(tmp_path / "lineage")
    store = SkillStore(
        tmp_path / "skills",
        lineage_index=services.index,
        revoke_queue=services.queue,
    )
    version = _version_with_source("r1")
    store.write_version(version)
    store._write_status_unchecked(bump_python_dep_status())
    status = quarantine_on_failures(
        store, version.skill_id, version.version, consecutive_failures=3
    )
    assert status.lifecycle == "quarantined"
    items = services.queue.drain(limit=20)
    kinds = {(i["source_kind"], i["source_id"]) for i in items}
    assert ("skill", f"{version.skill_id}@{version.version}") in kinds
    assert ("run", "r1") in kinds


def test_recertify_drains_queue(tmp_path: Path) -> None:
    from recertia.jobs.workers import recertify_with_revokes

    services = LineageServices.open(tmp_path / "lineage")
    store = SkillStore(
        tmp_path / "skills",
        lineage_index=services.index,
        revoke_queue=services.queue,
    )
    version = _version_with_source("poison-run")
    store.write_version(version)
    store._write_status_unchecked(bump_python_dep_status())
    services.queue.enqueue(source_kind="run", source_id="poison-run", reason="test")
    proposals = recertify_with_revokes(
        store,
        lineage_index=services.index,
        revoke_queue=services.queue,
        max_writes=10,
    )
    assert any(p.skill_id == version.skill_id for p in proposals)
    assert store.get_status(version.skill_id, version.version).lifecycle == "needs_recert"
    assert services.queue.drain(limit=10) == []


def test_r13_still_warning_miner_draft_clean() -> None:
    draft = draft_from_mine_proposal(
        Proposal(kind="mine", skill_id="mined-demo-skill", version=1, rationale="hint")
    )
    report = lint_report(
        draft,
        SkillStatus(skill_id=draft.skill_id, version=1, lifecycle="draft"),
        skip_if_hash_matches=False,
    )
    codes = {f.code: f.severity for f in report.findings}
    assert "R1.3" not in codes
    # Severity of the rule itself is unchanged when it does fire.
    bare = SkillVersion(
        skill_id="bare-packaging-skill",
        version=1,
        title="A packaged demo skill here",
        intent="Handle the packaged chore in a generic reusable way for this class.",
        task_class="repo-chore",
        steps=[Step(id="step_1", tool="shell", intent="Run the packaged command")],
        certification_criteria=draft.certification_criteria,
        provenance=Provenance(
            distilled_from_run="r", distilled_at=datetime.now(timezone.utc)
        ),
        hygiene=Hygiene(secret_scan="passed", scanned_at=datetime.now(timezone.utc)),
    )
    fired = lint_report(
        bare,
        SkillStatus(skill_id=bare.skill_id, version=1, lifecycle="draft"),
        skip_if_hash_matches=False,
    )
    assert any(f.code == "R1.3" and f.severity == "warning" for f in fired.findings)


def test_orchestrator_sets_deterministic_guide_from_policy(tmp_path: Path) -> None:
    with closing(GraphOrchestrator(tmp_path / "runs-off")) as off:
        assert off.policy is None
    policy = Policy(
        version="p-test",
        authoring_prior_version="ap-test",
    )
    with closing(
        GraphOrchestrator(
            tmp_path / "runs-on",
            policy=policy.model_copy(
                update={"improvement": policy.improvement.model_copy(
                    update={"deterministic_guide": True}
                )}
            ),
        )
    ) as on:
        assert on.policy is not None
        assert on.policy.improvement.deterministic_guide is True
