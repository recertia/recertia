"""EvalStore success vectors and snapshot rates for variance-aware lift."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.eval import BinomialSample, EvalObservation
from recertia.evals.statistics import causal_lift
from recertia.evals.store import EvalStore, ObservationError


def _obs(
    run_id: str,
    *,
    arm: str,
    snapshot_id: str,
    success: bool,
    strategy: str | None = None,
    fixture: bool = False,
) -> EvalObservation:
    return EvalObservation(
        run_id=run_id,
        task_class="repo-chore",
        arm=arm,  # type: ignore[arg-type]
        snapshot_id=snapshot_id,
        first_attempt_success=success,
        is_eval_fixture=fixture,
        recorded_at=datetime.now(timezone.utc),
        evidence_hash=f"hash-{run_id}",
        strategy=strategy,
    )


def test_success_vectors_and_snapshot_rates(tmp_path: Path) -> None:
    store = EvalStore(tmp_path / "evals.db")
    rows = [
        _obs("t1", arm="treatment", snapshot_id="s1", success=True),
        _obs("t2", arm="treatment", snapshot_id="s1", success=False),
        _obs("t3", arm="treatment", snapshot_id="s2", success=True),
        _obs("c1", arm="control", snapshot_id="s1", success=False),
        _obs("c2", arm="control", snapshot_id="s2", success=False),
        _obs(
            "f1",
            arm="treatment",
            snapshot_id="s1",
            success=True,
            strategy="faithfulness:empty",
            fixture=True,
        ),
    ]
    for row in rows:
        store._append_observation(row)

    vectors = store.success_vectors(task_class="repo-chore")
    assert vectors["treatment"] == [1.0, 0.0, 1.0]
    snaps = store.snapshot_rates(task_class="repo-chore")
    assert snaps["treatment"] == [0.5, 1.0]
    t_rates, c_rates = store.arm_rate_series(task_class="repo-chore")
    assert t_rates == [0.5, 1.0]
    counts = store.arm_counts(task_class="repo-chore")
    assert counts["treatment"] == BinomialSample(successes=2, trials=3)
    result = causal_lift(
        counts["treatment"],
        counts.get("control", BinomialSample(successes=0, trials=0)),
        treatment_rates=t_rates,
        control_rates=c_rates,
        min_independent_runs=2,
    )
    assert result.treatment_variance is not None
    assert result.treatment_variance.best_worst_gap == 0.5
    store.close()


def test_misaligned_snapshots_pair_on_intersection(tmp_path: Path) -> None:
    store = EvalStore(tmp_path / "evals.db")
    store._append_observation(_obs("t1", arm="treatment", snapshot_id="s1", success=True))
    store._append_observation(_obs("t2", arm="treatment", snapshot_id="s2", success=True))
    store._append_observation(_obs("t3", arm="treatment", snapshot_id="s3", success=False))
    store._append_observation(_obs("c1", arm="control", snapshot_id="s2", success=False))
    store._append_observation(_obs("c2", arm="control", snapshot_id="s3", success=False))
    store._append_observation(_obs("c3", arm="control", snapshot_id="s4", success=True))
    _t, _c, paired, kind = store.variance_inputs(task_class="repo-chore")
    assert kind == "snapshot"
    assert paired == [1.0, 0.0]
    store.close()


def test_contribution_samples_ignore_faithfulness_rows(tmp_path: Path) -> None:
    from contracts.criteria import CriterionResult, TaskCriterion
    from contracts.run import RunManifest, RunState, SkillCandidateRef, Task

    store = EvalStore(tmp_path / "evals.db")
    now = datetime.now(timezone.utc)
    criterion = TaskCriterion(id="ok", kind="command", run="true", source="caller")

    def make_state(run_id: str, *, arm: str) -> RunState:
        return RunState(
            run_id=run_id,
            task=Task(
                task_id=run_id,
                request="sample",
                task_class="repo-chore",
                submitted_at=now,
            ),
            manifest=RunManifest(index_snapshot_id="snap", criteria_hash="locked"),
            arm=arm,  # type: ignore[arg-type]
            criteria=[criterion],
            criteria_locked_at=now,
            chosen=SkillCandidateRef(skill_id="s", version=1, score=1.0),
            attempt_no=1,
            results=[CriterionResult(criterion_id="ok", kind="command", passed=True)],
            terminal="solved",
        )

    store.append_run(make_state("shadow-ok", arm="shadow"))
    store.append_run(
        make_state("shadow-faith", arm="shadow"),
        strategy_override="faithfulness:empty",
    )
    shadow, suppression = store.contribution_samples(
        skill_id="s", version=1, task_class="repo-chore"
    )
    assert shadow.trials == 1
    assert suppression.trials == 0
    store.close()

def test_record_observation_still_refuses_caller_authored(tmp_path: Path) -> None:
    store = EvalStore(tmp_path / "evals.db")
    try:
        with pytest.raises(ObservationError):
            store.record_observation(_obs("x", arm="treatment", snapshot_id="s", success=True))
    finally:
        store.close()
