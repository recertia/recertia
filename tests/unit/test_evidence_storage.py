from __future__ import annotations

import multiprocessing
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.criteria import CriterionResult, SkillCertificationCriterion, TaskCriterion
from contracts.run import RunManifest, RunState, SkillCandidateRef, Task
from contracts.skill import Hygiene, Provenance, SkillVersion, Step
from recertia.evals.metrics import build_metric_report
from recertia.evals.store import EvalStore, ObservationError
from recertia.memory.procedural.allocate import allocate_and_write
from recertia.memory.procedural.store import SkillStore


def _state(
    run_id: str, *, arm: str, chosen: bool = False, suppressed: bool = False
) -> RunState:
    criterion = TaskCriterion(
        id="required-file",
        kind="command",
        run="test -f output.txt",
        source="caller",
    )
    return RunState(
        run_id=run_id,
        task=Task(
            task_id=run_id,
            request="create output.txt",
            task_class="repo-chore",
            submitted_at=datetime.now(timezone.utc),
        ),
        manifest=RunManifest(index_snapshot_id="snapshot-1", criteria_hash="locked"),
        arm=arm,  # type: ignore[arg-type]
        criteria=[criterion],
        criteria_locked_at=datetime.now(timezone.utc),
        chosen=(
            SkillCandidateRef(skill_id="evidence-skill", version=1, score=1.0) if chosen else None
        ),
        suppressed_skill=(
            SkillCandidateRef(skill_id="evidence-skill", version=1, score=1.0)
            if suppressed
            else None
        ),
        attempt_no=1,
        results=[CriterionResult(criterion_id="required-file", kind="command", passed=True)],
        terminal="solved",
    )


def _version() -> SkillVersion:
    now = datetime.now(timezone.utc)
    return SkillVersion(
        skill_id="process-safe",
        version=1,
        title="Process-safe allocation fixture",
        intent="A fixture used to verify durable allocation across worker processes.",
        task_class="repo-chore",
        steps=[Step(id="one", tool="shell", intent="No operation.", inputs={"command": "true"})],
        certification_criteria=[
            SkillCertificationCriterion(id="check", kind="command", run="true")
        ],
        provenance=Provenance(
            distilled_from_run="test",
            distilled_at=now,
            curation="human_authored",
            derivation="hand_authored",
        ),
        hygiene=Hygiene(secret_scan="passed", scanned_at=now),
    )


def _allocate_worker(root: str, result_path: str) -> None:
    try:
        version = allocate_and_write(SkillStore(root), _version())
        Path(result_path).write_text(str(version.version), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — child must leave a file, then fail
        Path(result_path).write_text(f"ERR {type(exc).__name__}: {exc}", encoding="utf-8")
        raise


def _reap_workers(workers: list[multiprocessing.Process]) -> None:
    for worker in workers:
        if worker.is_alive():
            worker.terminate()
    for worker in workers:
        worker.join(timeout=5)
        if worker.is_alive():
            worker.kill()
            worker.join(timeout=2)


def test_run_derived_observations_are_append_only_and_feed_metrics(tmp_path: Path) -> None:
    store = EvalStore(tmp_path / "evals.sqlite")
    treatment = _state("treatment-1", arm="treatment", chosen=True)
    control = _state("control-1", arm="control")
    shadow = _state("shadow-1", arm="shadow", chosen=True)
    suppression = _state("suppression-1", arm="control", suppressed=True)

    observation = store.append_run(treatment)
    store.append_run(control)
    store.append_run(shadow)
    store.append_run(suppression)
    assert observation.evidence_hash
    assert observation.valid_non_judge_evidence is True
    with pytest.raises(ObservationError, match="already has an immutable observation"):
        store.append_run(treatment)
    with pytest.raises(ObservationError, match="append_run-derived"):
        store.record_observation(observation)

    report = build_metric_report(
        store.metric_rows(task_class="repo-chore", snapshot_id="snapshot-1"),
        snapshot_id="snapshot-1",
        task_class="repo-chore",
    )
    assert report.first_attempt_success == 1.0
    assert report.causal_lift is not None
    enabled_sample, suppressed_sample = store.retrieval_ablation_samples(task_class="repo-chore")
    assert (enabled_sample.successes, enabled_sample.trials) == (1, 1)
    assert (suppressed_sample.successes, suppressed_sample.trials) == (2, 2)
    shadow_sample, suppression_sample = store.contribution_samples(
        skill_id="evidence-skill", version=1, task_class="repo-chore"
    )
    assert (shadow_sample.successes, shadow_sample.trials) == (1, 1)
    assert (suppression_sample.successes, suppression_sample.trials) == (1, 1)
    store.close()


def test_version_allocation_is_safe_across_processes(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    skills_root = tmp_path / "skills"
    result_dir = tmp_path / "results"
    result_dir.mkdir()
    workers = [
        ctx.Process(target=_allocate_worker, args=(str(skills_root), str(result_dir / f"{index}.txt")))
        for index in range(8)
    ]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=60)
        crashed = [(worker.pid, worker.exitcode) for worker in workers if worker.exitcode not in (0, None)]
        hung = [worker.pid for worker in workers if worker.is_alive()]
        payloads = []
        for index in range(len(workers)):
            path = result_dir / f"{index}.txt"
            payloads.append(path.read_text(encoding="utf-8") if path.is_file() else "missing")
        assert not hung, f"workers still running: {hung}; results={payloads}"
        assert not crashed, f"workers crashed: {crashed}; results={payloads}"
        versions = sorted(int(payload) for payload in payloads)
        assert versions == list(range(1, len(workers) + 1))
    finally:
        _reap_workers(workers)
