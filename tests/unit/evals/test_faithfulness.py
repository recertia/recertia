from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from contracts.criteria import SkillCertificationCriterion, TaskCriterion
from contracts.eval import BinomialSample
from contracts.run import RunManifest, RunState, Task
from contracts.skill import FailureMode, Hygiene, Provenance, SkillVersion, Step
from recertia.evals.faithfulness import (
    IntervenedSkillStore,
    bundle_hook_for,
    edit_distance,
    evaluate_faithfulness,
    event_kinds,
    jaccard,
    run_intervened_trials,
    strategy_tag,
    trajectory_divergence,
)
from recertia.evals.interventions import apply_corrupt, apply_empty, apply_filler, apply_irrelevant
from recertia.evals.store import EvalStore
from recertia.ledger import HashChainLedger
from recertia.validation.sensitivity import author_sensitivity_proof, empty_negative_fixture


def _skill(skill_id: str = "used-skill", task_class: str = "repo-chore") -> SkillVersion:
    now = datetime.now(timezone.utc)
    cert = SkillCertificationCriterion(
        id="ok",
        kind="command",
        run="test -f pyproject.toml",
        preregistered=True,
        sensitivity_proof=author_sensitivity_proof(
            SkillCertificationCriterion(
                id="ok", kind="command", run="test -f pyproject.toml", preregistered=True
            ),
            negative_workdir=empty_negative_fixture(),
        ),
    )
    return SkillVersion(
        skill_id=skill_id,
        version=1,
        title=f"Title for {skill_id} skill body",
        intent=f"Apply {skill_id} when pyproject.toml exists and the bump is a patch release.",
        task_class=task_class,
        steps=[
            Step(
                id="step_1",
                tool="shell",
                intent="Bump the pinned dependency and refresh the lockfile",
                inputs={"command": "pip install -U package==1.2.3"},
            )
        ],
        certification_criteria=[cert],
        failure_modes=[
            FailureMode(
                symptom="Lockfile stays stale after a no-op bump",
                response="Force a lockfile rewrite and re-run the installer",
            )
        ],
        provenance=Provenance(distilled_from_run="r", distilled_at=now),
        hygiene=Hygiene(secret_scan="passed", scanned_at=now),
    )


def test_empty_drops_body_but_keeps_structure() -> None:
    original = _skill()
    emptied = apply_empty(original)
    assert emptied.skill_id == original.skill_id
    assert emptied.steps[0].id == original.steps[0].id
    assert emptied.steps[0].tool == "shell"
    assert emptied.steps[0].intent != original.steps[0].intent
    assert emptied.failure_modes == []


def test_corrupt_mutates_key_fields() -> None:
    original = _skill()
    corrupted = apply_corrupt(original)
    assert corrupted.steps[0].id == original.steps[0].id
    assert corrupted.steps[0].intent != original.steps[0].intent
    assert corrupted.failure_modes[0].symptom != original.failure_modes[0].symptom


def test_filler_is_non_semantic_same_length() -> None:
    original = _skill()
    filled = apply_filler(original)
    assert len(filled.steps[0].intent) == len(original.steps[0].intent)
    assert "Bump" not in filled.steps[0].intent


def test_irrelevant_swaps_distant_body() -> None:
    original = _skill()
    donor = _skill(skill_id="other-domain", task_class="research-synthesis")
    donor = donor.model_copy(
        update={
            "steps": [
                donor.steps[0].model_copy(
                    update={"intent": "Synthesize the brief from the notes corpus"}
                )
            ]
        }
    )
    swapped = apply_irrelevant(original, donor)
    assert swapped.skill_id == original.skill_id
    assert swapped.steps[0].intent == "Synthesize the brief from the notes corpus"


def test_used_skill_intervention_is_detectable() -> None:
    skill = _skill()
    report = evaluate_faithfulness(
        skill=skill,
        baseline=BinomialSample(successes=8, trials=10),
        baseline_events=["retrieval_result", "plan_choice", "step_started", "terminal"],
        outcomes={
            "empty": BinomialSample(successes=2, trials=10),
            "corrupt": BinomialSample(successes=3, trials=10),
            "filler": BinomialSample(successes=2, trials=10),
        },
        events={
            "empty": ["retrieval_result", "plan_choice", "terminal"],
            "corrupt": ["retrieval_result", "plan_choice", "step_started", "failure_classified", "terminal"],
            "filler": ["retrieval_result", "terminal"],
        },
        skill_used=True,
        min_independent_runs=5,
    )
    assert report.score is not None and report.score > 0
    assert all(arm.detectable_change for arm in report.arms)
    assert all(arm.strategy.startswith("faithfulness:") for arm in report.arms)


def test_unused_skill_intervention_is_near_zero() -> None:
    skill = _skill("never-applied")
    events = ["retrieval_result", "plan_choice", "terminal"]
    report = evaluate_faithfulness(
        skill=skill,
        baseline=BinomialSample(successes=5, trials=10),
        baseline_events=events,
        outcomes={"empty": BinomialSample(successes=5, trials=10)},
        events={"empty": events},
        skill_used=False,
        min_independent_runs=5,
    )
    assert report.score == 0.0
    assert report.arms[0].divergence.jaccard == 1.0
    assert report.arms[0].divergence.edit_distance == 0
    assert report.arms[0].detectable_change is False


def test_jaccard_and_edit_distance() -> None:
    assert jaccard(["a", "b"], ["a", "b"]) == 1.0
    assert edit_distance(["a", "b", "c"], ["a", "x", "c"]) == 1
    div = trajectory_divergence(["a", "b"], ["a"])
    assert div.jaccard < 1.0
    assert div.edit_distance == 1
    assert div.normalized_edit == 0.5


def test_ledger_tag_cannot_be_mistaken_for_lift(tmp_path: Path) -> None:
    ledger = HashChainLedger(tmp_path / "ledger.jsonl")
    ledger.append(
        actor="recertia-faithfulness",
        action="faithfulness_report",
        target="used-skill@v1",
        evidence={"score": 1.0, "tagged": True, "production_path": False},
        at=datetime.now(timezone.utc),
    )
    entry = ledger.entries()[0]
    assert entry.action == "faithfulness_report"
    assert entry.evidence["production_path"] is False
    assert strategy_tag("empty") == "faithfulness:empty"


def test_event_kinds_from_trajectory_objects() -> None:
    class _Ev:
        def __init__(self, kind: str) -> None:
            self.event_kind = kind

    assert event_kinds([_Ev("retrieval_result"), "plan_choice", _Ev("terminal")]) == [
        "retrieval_result",
        "plan_choice",
        "terminal",
    ]


def test_intervened_store_replaces_target_body_only() -> None:
    class _Inner:
        def __init__(self, skill: SkillVersion) -> None:
            self.skill = skill

        def get_version(self, skill_id: str, version: int) -> SkillVersion:
            return self.skill

    original = _skill()
    overlay = IntervenedSkillStore(
        _Inner(original),
        skill_id=original.skill_id,
        version=1,
        intervention="empty",
    )
    emptied = overlay.get_version(original.skill_id, 1)
    assert emptied.steps[0].intent != original.steps[0].intent
    assert emptied.failure_modes == []


def test_bundle_hook_for_irrelevant_swaps_identity() -> None:
    from contracts.run import MemoryBundle, SkillCandidateRef

    hook = bundle_hook_for(
        skill_id="used-skill",
        version=1,
        intervention="irrelevant",
        donor_id="other-domain",
        donor_version=2,
    )
    bundle = MemoryBundle(
        skills=[SkillCandidateRef(skill_id="used-skill", version=1, score=0.9)]
    )
    swapped = hook(bundle)
    assert swapped.skills[0].skill_id == "other-domain"
    assert swapped.skills[0].version == 2


def test_zero_trial_arm_is_not_scored() -> None:
    skill = _skill()
    report = evaluate_faithfulness(
        skill=skill,
        baseline=BinomialSample(successes=8, trials=10),
        baseline_events=["retrieval_result", "plan_choice", "terminal"] * 8,
        outcomes={
            "empty": BinomialSample(successes=0, trials=0),
            "corrupt": BinomialSample(successes=0, trials=0),
            "filler": BinomialSample(successes=0, trials=0),
        },
        events={"empty": [], "corrupt": [], "filler": []},
        skill_used=True,
        min_independent_runs=5,
    )
    assert report.score is None
    assert report.scored_arms == 0
    assert all(arm.scored is False for arm in report.arms)
    assert all(arm.detectable_change is False for arm in report.arms)


def test_pairwise_same_kinds_different_bag_length_is_not_detectable() -> None:
    skill = _skill()
    kinds = ["retrieval_result", "plan_choice", "terminal"]
    report = evaluate_faithfulness(
        skill=skill,
        baseline=BinomialSample(successes=2, trials=2),
        baseline_event_groups={"fx-a": kinds, "fx-b": kinds},
        outcomes={"empty": BinomialSample(successes=2, trials=2)},
        event_groups={"empty": {"fx-a": kinds, "fx-b": kinds}},
        skill_used=True,
        min_independent_runs=5,
    )
    assert report.score == 0.0
    assert report.arms[0].divergence.jaccard == 1.0
    assert report.arms[0].detectable_change is False


def test_irrelevant_without_donor_does_not_raise() -> None:
    skill = _skill()
    report = evaluate_faithfulness(
        skill=skill,
        baseline=BinomialSample(successes=4, trials=8),
        baseline_events=["terminal"],
        outcomes={"irrelevant": BinomialSample(successes=4, trials=8)},
        events={"irrelevant": ["terminal"]},
        donor=None,
        skill_used=True,
        min_independent_runs=5,
    )
    assert report.scored_arms == 1
    assert report.arms[0].intervention == "irrelevant"


def test_run_intervened_trials_tags_eval_fixture_rows(tmp_path: Path) -> None:
    skill = _skill()

    class _Inner:
        def get_version(self, skill_id: str, version: int) -> SkillVersion:
            del skill_id, version
            return skill

    eval_store = EvalStore(tmp_path / "evals.db")
    seen: list[object] = []

    def runner(run_id: str, fixture_id: str, *, overlay, bundle_hook):
        body = overlay.get_version(skill.skill_id, skill.version)
        seen.append((body.steps[0].intent, bundle_hook))
        now = datetime.now(timezone.utc)
        return RunState(
            run_id=run_id,
            task=Task(
                task_id=fixture_id,
                request="faithfulness fixture",
                task_class="repo-chore",
                submitted_at=now,
                is_eval_fixture=True,
            ),
            manifest=RunManifest(index_snapshot_id="faithfulness", criteria_hash="locked"),
            criteria=[TaskCriterion(id="ok", kind="command", run="true", source="caller")],
            criteria_locked_at=now,
            attempt_no=1,
            terminal="unsolved",
        )

    rows = run_intervened_trials(
        skill=skill,
        intervention="empty",
        fixture_ids=["fx-1"],
        eval_store=eval_store,
        inner_store=_Inner(),
        runner=runner,
    )
    assert len(rows) == 1
    obs = rows[0]
    assert obs.strategy == "faithfulness:empty"
    assert obs.is_eval_fixture is True
    assert obs.fixture_id == "fx-1"
    counts = eval_store.arm_counts(task_class="repo-chore")
    assert counts == {}
    assert seen
    assert "placeholder" in seen[0][0]  # type: ignore[index]
    eval_store.close()


def test_faithfulness_cli_unavailable_without_trials(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from recertia.cli.main import app
    from recertia.memory.procedural.store import SkillStore

    store = SkillStore(tmp_path / "skills")
    skill = _skill()
    store.write_version(skill)
    db = tmp_path / "evals.db"
    result = CliRunner().invoke(
        app,
        [
            "faithfulness",
            "run",
            "--skill-id",
            skill.skill_id,
            "--skills-root",
            str(tmp_path / "skills"),
            "--eval-db",
            str(db),
            "--interventions",
            "empty,corrupt,filler",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "unavailable" in result.output
