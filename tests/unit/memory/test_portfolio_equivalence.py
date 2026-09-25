"""The portfolio controller is the only active-set path (RW-PC).

The differential suite that compared legacy vs controller served its purpose:
``docs/architecture/portfolio-measurement.md``. This module keeps the expiry
guard and the controller properties that report still relies on.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from contracts.criteria import (
    CriterionResult,
    SkillCertificationCriterion,
    TaskCriterion,
    mint_rejecting_proof,
)
from contracts.run import RunManifest, RunState, SkillCandidateRef, Task
from contracts.skill import Hygiene, Provenance, SkillVersion, Step
from contracts.stats import Contribution, PredictiveTrust, RetrievalAblationEffect, SkillStats
from contracts.status import SkillStatus
from recertia.evals.store import EvalStore
from recertia.memory.procedural import active_set
from recertia.memory.procedural.active_set import recompute_active_set
from recertia.memory.procedural.seeds import seed_approved_for_tests
from recertia.memory.procedural.store import SkillStore
from recertia.review.autonomy_config import AutonomyConfig

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
_MEASUREMENT_REPORT = (
    Path(__file__).resolve().parents[3] / "docs" / "architecture" / "portfolio-measurement.md"
)


def _version(skill_id: str, *, version: int = 1, task_class: str = "repo-chore") -> SkillVersion:
    base = SkillCertificationCriterion(id="ok", kind="command", run="true", preregistered=True)
    return SkillVersion(
        skill_id=skill_id,
        version=version,
        title=f"Equivalence fixture {skill_id}",
        intent=f"Intent text long enough for the {skill_id} equivalence fixture version.",
        task_class=task_class,
        steps=[
            Step(
                id="step_1",
                tool="shell",
                intent="Run a trivial shell step for the equivalence fixture",
                inputs={"command": "true"},
            )
        ],
        certification_criteria=[
            base.model_copy(
                update={"sensitivity_proof": mint_rejecting_proof(base, fingerprint="equivalence")}
            )
        ],
        provenance=Provenance(
            distilled_from_run="equivalence",
            distilled_at=_NOW,
            curation="human_authored",
            authoring_prior_version="ap-test",
        ),
        hygiene=Hygiene(secret_scan="passed", scanned_at=_NOW),
    )


def _contribution(estimate: float | None, *, applications: int) -> Contribution:
    if estimate is None:
        return Contribution(applications=applications, successes=applications // 2)
    return Contribution(
        applications=applications,
        successes=round(applications * (0.5 + estimate)),
        suppressed_applications=applications,
        suppressed_successes=applications // 2,
    )


class _Row:
    def __init__(
        self,
        skill_id: str,
        *,
        lifecycle: str = "approved",
        version: int = 1,
        task_class: str = "repo-chore",
        estimate: float | None = 0.2,
        applications: int = 100,
        trust: tuple[int, int] = (10, 5),
        last_used_at: datetime | None = None,
        active: bool = False,
    ) -> None:
        self.skill_id = skill_id
        self.lifecycle = lifecycle
        self.version = version
        self.task_class = task_class
        self.estimate = estimate
        self.applications = applications
        self.trust = trust
        self.last_used_at = last_used_at
        self.active = active


_LIBRARY = (
    _Row("eq-a", estimate=0.3, last_used_at=_NOW - timedelta(days=1), active=True),
    _Row("eq-b", estimate=0.3, trust=(10, 9), last_used_at=_NOW - timedelta(days=2)),
    _Row("eq-c", estimate=0.3, last_used_at=_NOW - timedelta(days=1)),
    _Row("eq-h", estimate=0.3, last_used_at=_NOW - timedelta(days=1), active=True),
    _Row("eq-d", estimate=0.1, last_used_at=None),
    _Row("eq-e", estimate=None, trust=(10, 10), applications=60, last_used_at=_NOW),
    _Row("eq-f", estimate=None, trust=(10, 10), applications=60, last_used_at=_NOW),
    _Row("eq-g", estimate=-0.2, trust=(20, 4), applications=200, active=True),
    _Row("multi", version=1, estimate=0.05, applications=40),
    _Row("multi", version=2, estimate=0.45, applications=40),
    _Row("sh-one", lifecycle="shadow", estimate=0.4),
    _Row("bench-one", lifecycle="benched", estimate=-0.5, applications=300),
    _Row("cand-one", lifecycle="candidate"),
    _Row("quar-one", lifecycle="quarantined"),
    _Row("docs-x", task_class="docs-chore", estimate=0.3),
    _Row("docs-y", task_class="docs-chore", estimate=0.1),
    _Row("docs-z", task_class="docs-chore", lifecycle="benched", estimate=None),
)

_EVIDENCED: dict[tuple[str, int], tuple[tuple[int, int], tuple[int, int]]] = {
    ("eq-a", 1): ((6, 10), (4, 10)),
    ("eq-b", 1): ((8, 10), (4, 10)),
    ("eq-c", 1): ((6, 10), (4, 10)),
    ("eq-g", 1): ((2, 10), (6, 10)),
    ("multi", 2): ((9, 10), (4, 10)),
    ("multi", 1): ((5, 10), (0, 0)),
}


class _SpySkillStore(SkillStore):
    def __init__(self, skills_root: Path | str) -> None:
        super().__init__(skills_root)
        self.status_writes: list[dict] = []

    def write_status(self, status: SkillStatus, *, expected_lifecycle: str | None = None) -> Path:
        self.status_writes.append(status.model_dump(mode="json"))
        return super().write_status(status, expected_lifecycle=expected_lifecycle)


class _SpyEvalStore(EvalStore):
    def __init__(self, path: Path | str) -> None:
        super().__init__(path)
        self.ablation_writes: list[dict] = []

    def write_retrieval_ablation(self, effect: RetrievalAblationEffect) -> None:
        payload = effect.model_dump(mode="json")
        payload.pop("last_evaluated_at", None)
        self.ablation_writes.append(payload)
        super().write_retrieval_ablation(effect)


def _build_library(root: Path, rows: tuple[_Row, ...] = _LIBRARY) -> _SpySkillStore:
    store = _SpySkillStore(root)
    for row in rows:
        version = _version(row.skill_id, version=row.version, task_class=row.task_class)
        if row.lifecycle == "approved":
            seed_approved_for_tests(store, version, active=row.active)
        else:
            store.write_version(version)
            store._write_status_unchecked(
                SkillStatus(
                    skill_id=row.skill_id,
                    version=row.version,
                    lifecycle=row.lifecycle,  # type: ignore[arg-type]
                    active=False,
                )
            )
        store.write_stats(
            SkillStats(
                skill_id=row.skill_id,
                version=row.version,
                predictive_trust=PredictiveTrust(
                    applications=row.trust[0],
                    successes=row.trust[1],
                    last_used_at=row.last_used_at,
                ),
                contribution=_contribution(row.estimate, applications=row.applications),
            )
        )
    store.status_writes.clear()
    return store


def _observation(
    run_id: str,
    *,
    arm: str,
    task_class: str,
    solved: bool,
    chosen: tuple[str, int] | None = None,
    suppressed: tuple[str, int] | None = None,
) -> RunState:
    criterion = TaskCriterion(id="req", kind="command", run="true", source="caller")
    return RunState(
        run_id=run_id,
        task=Task(
            task_id=run_id,
            request="equivalence evidence fixture",
            task_class=task_class,
            submitted_at=_NOW,
        ),
        manifest=RunManifest(index_snapshot_id="equivalence-snapshot", criteria_hash="locked"),
        arm=arm,  # type: ignore[arg-type]
        criteria=[criterion],
        criteria_locked_at=_NOW,
        chosen=SkillCandidateRef(skill_id=chosen[0], version=chosen[1], score=1.0)
        if chosen
        else None,
        suppressed_skill=SkillCandidateRef(skill_id=suppressed[0], version=suppressed[1], score=1.0)
        if suppressed
        else None,
        attempt_no=1,
        results=[CriterionResult(criterion_id="req", kind="command", passed=solved)],
        terminal="solved" if solved else "unsolved",
    )


def _build_evidence(path: Path) -> _SpyEvalStore:
    store = _SpyEvalStore(path)
    for (skill_id, version), (shadow, suppression) in _EVIDENCED.items():
        successes, trials = shadow
        for i in range(trials):
            store.append_run(
                _observation(
                    f"sh-{skill_id}-{version}-{i}",
                    arm="shadow",
                    task_class="repo-chore",
                    solved=i < successes,
                    chosen=(skill_id, version),
                )
            )
        successes, trials = suppression
        for i in range(trials):
            store.append_run(
                _observation(
                    f"su-{skill_id}-{version}-{i}",
                    arm="control",
                    task_class="repo-chore",
                    solved=i < successes,
                    suppressed=(skill_id, version),
                )
            )
    for i in range(10):
        store.append_run(
            _observation(f"tr-{i}", arm="treatment", task_class="repo-chore", solved=i < 7)
        )
    for i in range(10):
        store.append_run(
            _observation(f"co-{i}", arm="control", task_class="repo-chore", solved=i < 5)
        )
    store.ablation_writes.clear()
    return store


def test_flag_and_legacy_branch_do_not_outlive_phase_2() -> None:
    """Phase 2 is reported; the dual path must be gone."""

    assert _MEASUREMENT_REPORT.exists()
    assert not hasattr(active_set, "_recompute_active_set_legacy"), (
        "Phase 2 is reported but the legacy ranking branch is still present. Delete "
        "_recompute_active_set_legacy and fold the controller path into recompute_active_set."
    )
    assert not hasattr(active_set, "_portfolio_controller_enabled"), (
        "Phase 2 is reported but RECERTIA_PORTFOLIO_CONTROLLER is still read. Delete the flag; "
        "the pure controller is the only path."
    )


def test_recompute_never_benches(tmp_path: Path) -> None:
    store = _build_library(tmp_path / "skills")
    recompute_active_set(store, config=AutonomyConfig(evidence_floor=1, retirement_threshold=0.0))
    for _version, status, _stats in store.iter_loaded():
        assert status.retirement.benched_at is None
        assert status.retirement.reason is None
    lifecycles = {
        (version.skill_id, version.version): status.lifecycle
        for version, status, _stats in store.iter_loaded()
    }
    assert lifecycles[("eq-g", 1)] == "approved"
    assert lifecycles[("bench-one", 1)] == "benched"


def test_cap_binds_and_writes(tmp_path: Path) -> None:
    store = _build_library(tmp_path / "skills")
    _updated, pressure = recompute_active_set(
        store, config=AutonomyConfig(active_cap_per_task_class=3)
    )
    active = {
        (version.skill_id, version.version)
        for version, status, _stats in store.iter_loaded()
        if version.task_class == "repo-chore" and status.active
    }
    approved = {
        (version.skill_id, version.version)
        for version, status, _stats in store.iter_loaded()
        if version.task_class == "repo-chore" and status.lifecycle == "approved"
    }
    assert 0 < len(active) <= 3 < len(approved)
    assert pressure["repo-chore"] > 0
    assert store.status_writes


def test_eval_store_narrows_to_evidenced_skills(tmp_path: Path) -> None:
    eval_store = _build_evidence(tmp_path / "evals.sqlite")
    try:
        bulk = eval_store.contribution_samples_bulk(task_class="repo-chore")
        both_arms = {key for key, (sh, su) in bulk.items() if sh.trials and su.trials}
        one_arm = {key for key, (sh, su) in bulk.items() if not (sh.trials and su.trials)}
        assert ("eq-a", 1) in both_arms
        assert ("multi", 1) in one_arm
        assert ("eq-d", 1) not in bulk
        assert eval_store.contribution_samples_bulk(task_class="docs-chore") == {}
    finally:
        eval_store.close()


def test_recency_breaks_estimate_and_trust_ties(tmp_path: Path) -> None:
    rows = (
        _Row("aa-stale", estimate=0.2, last_used_at=_NOW - timedelta(days=10)),
        _Row("zz-fresh", estimate=0.2, last_used_at=_NOW),
    )
    store = _build_library(tmp_path / "skills", rows)
    recompute_active_set(store, config=AutonomyConfig(active_cap_per_task_class=1))
    active = {
        (version.skill_id, version.version)
        for version, status, _stats in store.iter_loaded()
        if status.active
    }
    assert active == {("zz-fresh", 1)}


def test_version_tiebreak_is_numeric(tmp_path: Path) -> None:
    rows = (
        _Row("dup", version=2, estimate=0.2, last_used_at=_NOW),
        _Row("dup", version=10, estimate=0.2, last_used_at=_NOW),
    )
    store = _build_library(tmp_path / "skills", rows)
    recompute_active_set(store, config=AutonomyConfig(active_cap_per_task_class=1))
    active = {
        (version.skill_id, version.version)
        for version, status, _stats in store.iter_loaded()
        if status.active
    }
    assert active == {("dup", 2)}
