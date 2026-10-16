"""One job-name table for the CLI and the HTTP API."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Alias → canonical name. Both dispatchers branch on the canonical name.
_CANONICAL: dict[str, str] = {
    "mine": "mine",
    "miner": "mine",
    "curator": "curator",
    "curate": "curator",
    "practice": "practice",
    "recertify": "recertify",
    "recertifier": "recertify",
    "shadow": "shadow",
    "parallelise": "parallelise",
    "parallelize": "parallelise",
    "serialise": "serialise",
    "serialize": "serialise",
    "correction": "correction",
    "correction_miner": "correction",
    "hex": "hex",
    "practice_hex": "hex",
    "compress": "compress",
}


def canonical_job(name: str) -> str | None:
    """Return the shared job id, or None when the name is not registered."""

    return _CANONICAL.get(name.strip().lower())


def known_job_names() -> tuple[str, ...]:
    return tuple(sorted(set(_CANONICAL.values())))


@dataclass
class JobDispatch:
    """Inputs shared by the CLI and ``POST /v1/jobs/{job}/run``."""

    store: Any
    runner: Any
    budget: Any
    policy: Any
    runs_root: Path
    lineage: Any
    dry_run: bool = False
    one_off: list[str] | None = None
    tool_upgraded: str | None = None


def run_named_job(name: str, job: JobDispatch) -> Any | None:
    """Run a registered handler. ``None`` means this module does not own ``name``."""

    handler = _HANDLERS.get(name)
    if handler is None:
        return None
    return handler(job)


def _run_practice(job: JobDispatch) -> Any:
    from recertia.jobs.workers import (
        load_one_off_reasons,
        practice_from_fail_clusters,
        practice_from_one_offs,
    )
    from recertia.memory.episodic import EpisodicStore

    explicit = list(job.one_off) if job.one_off else None
    episodic = EpisodicStore(job.runs_root / "episodic")
    eligible = (
        episodic.clusters.eligible()
        if job.policy.improvement.fail_cluster_curriculum and not explicit
        else []
    )
    curriculum = None if job.dry_run else job.runs_root / "practice-curriculum"
    if eligible:
        return job.runner.run(
            "fail_cluster_author",
            lambda: practice_from_fail_clusters(eligible, curriculum_dir=curriculum),
            budget=job.budget,
        )
    reasons = explicit if explicit else load_one_off_reasons(job.runs_root / "one_off_log.jsonl")
    if not reasons:
        reasons = ["unsolved one-off cluster"]
    return job.runner.run(
        "practice",
        lambda: practice_from_one_offs(reasons, curriculum_dir=curriculum),
        budget=job.budget,
    )


def _run_recertify(job: JobDispatch) -> Any:
    from recertia.evals.store import EvalStore
    from recertia.jobs.workers import recertify_with_revokes

    eval_store = EvalStore(job.runs_root / "evals.db")
    try:
        return job.runner.run(
            "recertify",
            lambda: recertify_with_revokes(
                job.store,
                lineage_index=job.lineage.index,
                revoke_queue=job.lineage.queue,
                max_writes=job.runner.quota.max_status_writes_per_tick,
                tool_upgraded=job.tool_upgraded,
                eval_store=eval_store,
            ),
            budget=job.budget,
        )
    finally:
        eval_store.close()


def _run_shadow(job: JobDispatch) -> Any:
    from recertia.jobs.workers import schedule_shadow_evaluations

    return job.runner.run(
        "shadow",
        lambda: schedule_shadow_evaluations(job.store),
        budget=job.budget,
    )


def _run_compress(job: JobDispatch) -> Any:
    from recertia.jobs.workers import propose_compress

    return job.runner.run("compress", propose_compress, budget=job.budget)


_HANDLERS = {
    "practice": _run_practice,
    "recertify": _run_recertify,
    "shadow": _run_shadow,
    "compress": _run_compress,
}
