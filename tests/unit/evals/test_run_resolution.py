"""REC-011 C1 part 1: record run resolution. Decision paths must not read it."""

from __future__ import annotations

import inspect
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from contracts.criteria import CriterionResult, TaskCriterion
from contracts.eval import BinomialSample, EvalObservation
from contracts.run import RunManifest, RunState, Task
from recertia.app.evals import EvalService
from recertia.evals.metrics import build_metric_report
from recertia.evals.statistics import causal_lift
from recertia.evals.store import EvalStore, reported_resolution

_NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)


def _locked_state(
    run_id: str,
    *,
    terminal: str,
    exit_code: int | None,
    passed: bool,
    task_class: str = "resolution-rec",
    weight: float = 1.0,
) -> RunState:
    criterion = TaskCriterion(
        id="gate",
        kind="command",
        run="true",
        source="caller",
        weight=weight,
    )
    result = CriterionResult(
        criterion_id="gate",
        kind="command",
        passed=passed,
        exit_code=exit_code,
    )
    return RunState(
        run_id=run_id,
        task=Task(
            task_id=run_id,
            request="record resolution",
            task_class=task_class,
            submitted_at=_NOW,
        ),
        manifest=RunManifest(index_snapshot_id="snap-1", criteria_hash="locked"),
        criteria=[criterion],
        criteria_locked_at=_NOW,
        attempt_no=1,
        results=[result],
        terminal=terminal,  # type: ignore[arg-type]
    )


def _columns(store: EvalStore, run_id: str) -> tuple[str | None, int | None, str | None]:
    row = store._conn.execute(
        "SELECT resolution, exit_code, reason FROM observations WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    assert row is not None
    exit_code = row["exit_code"]
    return row["resolution"], (None if exit_code is None else int(exit_code)), row["reason"]


def _vector_obs(
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
        recorded_at=_NOW,
        evidence_hash=f"hash-{run_id}",
        strategy=strategy,
    )


def test_resolution_recorded_for_124_126_and_error(tmp_path: Path) -> None:
    store = EvalStore(tmp_path / "evals.db")

    passed = store.append_run(_locked_state("pass-1", terminal="solved", exit_code=0, passed=True))
    failed = store.append_run(_locked_state("fail-1", terminal="unsolved", exit_code=1, passed=False))
    timed_out = store.append_run(_locked_state("timeout-1", terminal="unsolved", exit_code=124, passed=False))
    unavailable = store.append_run(
        _locked_state("unavail-1", terminal="unsolved", exit_code=126, passed=False)
    )
    errored = store.append_run(_locked_state("error-1", terminal="error", exit_code=1, passed=False))
    optional_timeout = store.append_run(
        _locked_state(
            "advisory-124",
            terminal="unsolved",
            exit_code=124,
            passed=False,
            weight=0.5,
        )
    )

    assert passed.resolution == "decided" and passed.reason == "decided"
    assert failed.resolution == "decided" and failed.reason == "decided"
    assert timed_out.resolution == "unresolved"
    assert timed_out.exit_code == 124
    assert timed_out.reason == "required criterion timed out (exit 124)"
    assert unavailable.resolution == "unresolved"
    assert unavailable.exit_code == 126
    assert unavailable.reason == "required criterion unavailable (exit 126)"
    assert errored.resolution == "invalid"
    assert errored.exit_code == 1
    assert errored.reason == "terminal=error"
    assert optional_timeout.resolution == "decided"

    assert _columns(store, "timeout-1") == (
        "unresolved",
        124,
        "required criterion timed out (exit 124)",
    )
    assert _columns(store, "unavail-1") == (
        "unresolved",
        126,
        "required criterion unavailable (exit 126)",
    )
    assert _columns(store, "error-1") == ("invalid", 1, "terminal=error")
    assert _columns(store, "pass-1")[0] == "decided"
    assert _columns(store, "fail-1")[0] == "decided"

    listed = {obs.run_id: obs for obs in store.list_observations(task_class="resolution-rec")}
    assert listed["timeout-1"].resolution == "unresolved"
    assert listed["timeout-1"].exit_code == 124
    assert listed["error-1"].resolution == "invalid"
    assert listed["error-1"].exit_code == 1
    assert listed["pass-1"].resolution == "decided"

    # Pre-REC-011 row: omit the new columns so SQLite stores NULL. Payload has no keys.
    store._conn.execute(
        """
        INSERT INTO observations (
            run_id, task_class, arm, snapshot_id, first_attempt_success,
            is_eval_fixture, valid_non_judge_evidence, evidence_hash,
            recorded_at, payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "legacy-1",
            "resolution-rec",
            "treatment",
            "snap-1",
            0,
            0,
            1,
            "legacy-hash",
            _NOW.isoformat(),
            json.dumps(
                {
                    "run_id": "legacy-1",
                    "task_class": "resolution-rec",
                    "arm": "treatment",
                    "snapshot_id": "snap-1",
                    "first_attempt_success": False,
                    "recorded_at": _NOW.isoformat(),
                }
            ),
        ),
    )
    store._conn.commit()
    legacy_resolution, legacy_exit, legacy_reason = _columns(store, "legacy-1")
    assert legacy_resolution is None
    assert legacy_exit is None
    assert legacy_reason is None
    assert reported_resolution(legacy_resolution) == "legacy"
    assert reported_resolution(legacy_resolution) != "decided"
    legacy_obs = next(obs for obs in store.list_observations() if obs.run_id == "legacy-1")
    assert legacy_obs.resolution is None
    assert reported_resolution(legacy_obs.resolution) == "legacy"

    # Existing DB written before this change: ALTER TABLE adds NULL columns.
    pre = tmp_path / "pre.db"
    with sqlite3.connect(pre) as conn:
        conn.execute(
            """
            CREATE TABLE observations (
                run_id TEXT PRIMARY KEY,
                task_class TEXT NOT NULL,
                arm TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                model_version TEXT,
                first_attempt_success INTEGER NOT NULL,
                predicted_success REAL,
                terminal TEXT,
                fixture_id TEXT,
                is_eval_fixture INTEGER NOT NULL,
                strategy TEXT,
                attempt_no INTEGER,
                cost_usd REAL,
                abstention_confirmed INTEGER,
                skill_id TEXT,
                skill_version INTEGER,
                suppressed_skill_id TEXT,
                suppressed_skill_version INTEGER,
                valid_non_judge_evidence INTEGER NOT NULL DEFAULT 0,
                evidence_hash TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO observations (
                run_id, task_class, arm, snapshot_id, first_attempt_success,
                is_eval_fixture, valid_non_judge_evidence, evidence_hash,
                recorded_at, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "pre-1",
                "migrated",
                "treatment",
                "snap-1",
                1,
                0,
                1,
                "pre-hash",
                _NOW.isoformat(),
                json.dumps(
                    {
                        "run_id": "pre-1",
                        "task_class": "migrated",
                        "arm": "treatment",
                        "snapshot_id": "snap-1",
                        "first_attempt_success": True,
                        "recorded_at": _NOW.isoformat(),
                    }
                ),
            ),
        )
    migrated = EvalStore(pre)
    names = {row["name"] for row in migrated._conn.execute("PRAGMA table_info(observations)")}
    assert {"resolution", "exit_code", "reason"} <= names
    pre_resolution, pre_exit, pre_reason = _columns(migrated, "pre-1")
    assert pre_resolution is None and pre_exit is None and pre_reason is None
    assert reported_resolution(pre_resolution) == "legacy"
    assert migrated.arm_counts(task_class="migrated")["treatment"] == BinomialSample(successes=1, trials=1)
    migrated.close()

    # Recording only: 124/126/error/legacy still sit in the arm_counts denominator.
    counts = store.arm_counts(task_class="resolution-rec")
    assert counts["treatment"] == BinomialSample(successes=1, trials=7)

    # Non-interference: the store-vectors fixture still yields the same arm_counts / lift.
    vectors = EvalStore(tmp_path / "vectors.db")
    for row in (
        _vector_obs("t1", arm="treatment", snapshot_id="s1", success=True),
        _vector_obs("t2", arm="treatment", snapshot_id="s1", success=False),
        _vector_obs("t3", arm="treatment", snapshot_id="s2", success=True),
        _vector_obs("c1", arm="control", snapshot_id="s1", success=False),
        _vector_obs("c2", arm="control", snapshot_id="s2", success=False),
        _vector_obs(
            "f1",
            arm="treatment",
            snapshot_id="s1",
            success=True,
            strategy="faithfulness:empty",
            fixture=True,
        ),
    ):
        vectors._append_observation(row)
    vector_counts = vectors.arm_counts(task_class="repo-chore")
    assert vector_counts["treatment"] == BinomialSample(successes=2, trials=3)
    assert vector_counts["control"] == BinomialSample(successes=0, trials=2)
    t_rates, c_rates = vectors.arm_rate_series(task_class="repo-chore")
    assert t_rates == [0.5, 1.0]
    lift = causal_lift(
        vector_counts["treatment"],
        vector_counts["control"],
        treatment_rates=t_rates,
        control_rates=c_rates,
        min_independent_runs=2,
    )
    assert lift.treatment_variance is not None
    assert lift.treatment_variance.best_worst_gap == 0.5
    vectors.close()

    for method in (
        EvalStore.arm_counts,
        EvalStore.success_vectors,
        EvalStore.snapshot_rate_map,
        EvalStore.contribution_samples,
        EvalStore.contribution_samples_bulk,
        EvalStore.retrieval_ablation_samples,
        EvalStore.field_failure_streaks,
        causal_lift,
        build_metric_report,
        EvalService.lift,
    ):
        source = inspect.getsource(method)
        assert "resolution" not in source, f"{method.__qualname__} must not read resolution"

    store.close()


def test_invalid_terminal_error_keeps_exit_code(tmp_path: Path) -> None:
    store = EvalStore(tmp_path / "evals.db")
    failed = store.append_run(_locked_state("err-1", terminal="error", exit_code=1, passed=False))
    timed_out = store.append_run(_locked_state("err-124", terminal="error", exit_code=124, passed=False))
    missing = store.append_run(_locked_state("err-none", terminal="error", exit_code=None, passed=False))
    assert failed.resolution == "invalid" and failed.exit_code == 1
    assert timed_out.resolution == "invalid" and timed_out.exit_code == 124
    assert missing.resolution == "invalid" and missing.exit_code is None
    assert _columns(store, "err-1") == ("invalid", 1, "terminal=error")
    assert _columns(store, "err-124") == ("invalid", 124, "terminal=error")
    assert _columns(store, "err-none") == ("invalid", None, "terminal=error")
    store.close()
