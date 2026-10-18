"""One job-name table and dispatcher for the CLI and the HTTP API."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from recertia.app.authz import Resource
from recertia.errors import ConfigError, Forbidden, NotFound
from recertia.ports.identity import Principal


@dataclass(frozen=True)
class JobSpec:
    name: str
    aliases: tuple[str, ...]
    handler: Callable[[JobDispatch], Any]


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
    hint: list[str] | None = None
    arxiv_id: list[str] | None = None
    arxiv_query: str | None = None
    arxiv_max: int = 5
    with_pdf: bool = False
    pdf_sandbox: bool = False
    distill_paper: bool = False
    facts_root: Path | None = None
    submit: bool = False
    skill_id: str | None = None
    skill_version: int = 1
    fake_edge_failures: int = 0
    merge_conflicts: int = 0
    edits_log: Path | None = None
    task_class: str | None = None


def canonical_job(name: str) -> str | None:
    """Return the shared job id, or None when the name is not registered."""

    spec = _default_service().resolve(name)
    return spec.name if spec is not None else None


def known_job_names() -> tuple[str, ...]:
    return _default_service().known_names()


class JobService:
    """Resolve aliases and run the matching handler. The only job name table."""

    def __init__(
        self,
        *,
        pdp: Any | None = None,
        specs: tuple[JobSpec, ...] | None = None,
    ) -> None:
        self._pdp = pdp
        self._specs = specs if specs is not None else default_job_specs()
        by_alias: dict[str, JobSpec] = {}
        for spec in self._specs:
            for alias in (spec.name, *spec.aliases):
                by_alias[alias] = spec
        self._by_alias = by_alias

    def resolve(self, name: str) -> JobSpec | None:
        return self._by_alias.get(name.strip().lower())

    def known_names(self) -> tuple[str, ...]:
        return tuple(sorted({spec.name for spec in self._specs}))

    def run(
        self,
        principal: Principal,
        name: str,
        *,
        dispatch: JobDispatch,
        console_roles: frozenset[str] | None = None,
    ) -> Any:
        spec = self.resolve(name)
        if spec is None:
            raise NotFound(f"unknown job {name}")
        self._require(principal, console_roles=console_roles)
        return spec.handler(dispatch)

    def _require(
        self,
        principal: Principal,
        *,
        console_roles: frozenset[str] | None,
    ) -> None:
        pdp = self._pdp
        if pdp is None:
            return
        if console_roles is not None:
            if console_roles & {"reviewer", "admin", "platform_admin"}:
                return
            raise Forbidden("requires role reviewer")
        pdp.require(principal, "jobs.run", Resource(tenant_id=principal.tenant_id))


def default_job_specs() -> tuple[JobSpec, ...]:
    return (
        JobSpec("mine", ("miner",), _run_mine),
        JobSpec("curator", ("curate",), _run_curator),
        JobSpec("practice", (), _run_practice),
        JobSpec("recertify", ("recertifier",), _run_recertify),
        JobSpec("shadow", (), _run_shadow),
        JobSpec("parallelise", ("parallelize",), _run_parallelise),
        JobSpec("serialise", ("serialize",), _run_serialise),
        JobSpec("correction", ("correction_miner",), _run_correction),
        JobSpec("fail_cluster", ("fail_cluster_author",), _run_fail_cluster),
        JobSpec("hex", ("practice_hex",), _run_hex),
        JobSpec("compress", (), _run_compress),
    )


def _run(job: JobDispatch, job_name: str, fn: Callable[[], Any]) -> Any:
    return job.runner.run(job_name, fn, budget=job.budget, task_class=job.task_class)


def _run_mine(job: JobDispatch) -> Any:
    from recertia.jobs.workers import (
        enqueue_mined_candidate,
        mine_from_arxiv,
        mine_from_repo_hints,
    )

    use_arxiv = bool(job.arxiv_id) or bool(job.arxiv_query and job.arxiv_query.strip())
    if use_arxiv:

        def _mine_arxiv() -> list:
            return mine_from_arxiv(
                job.store,
                arxiv_ids=list(job.arxiv_id or []),
                query=job.arxiv_query,
                max_results=job.arxiv_max,
            )

        result = _run(job, "mine", _mine_arxiv)
        if job.with_pdf and result.proposals:
            from recertia.jobs.paper_pipeline import enrich_proposals_with_pdf

            result.proposals = enrich_proposals_with_pdf(
                result.proposals,
                dest_dir=job.runs_root / "arxiv-pdfs",
                use_sandbox=job.pdf_sandbox,
                sandbox_workdir=job.runs_root / "arxiv-pdf-sandbox",
            )
    else:
        hints = list(job.hint or ["README.md chore hints"])
        result = _run(
            job, "mine", lambda: mine_from_repo_hints(job.store, hints=hints)
        )
    if job.submit and not job.dry_run:
        if job.distill_paper and use_arxiv:
            from recertia.jobs.paper_pipeline import submit_paper_proposals
            from recertia.memory.semantic import FactStore

            fact_store = FactStore(job.facts_root or job.runs_root / "facts")
            written = submit_paper_proposals(
                job.store,
                result.proposals,
                fact_store=fact_store,
                distill=True,
            )
            for proposal, (draft, facts) in zip(result.proposals, written, strict=False):
                proposal.payload = {
                    **(proposal.payload or {}),
                    "submitted_skill_id": draft.skill_id,
                    "submitted_version": draft.version,
                    "facts": len(facts),
                }
        else:
            for proposal in result.proposals:
                draft = enqueue_mined_candidate(job.store, proposal)
                proposal.payload = {
                    **(proposal.payload or {}),
                    "submitted_skill_id": draft.skill_id,
                    "submitted_version": draft.version,
                }
    return result


def _run_curator(job: JobDispatch) -> Any:
    from recertia.evals.store import EvalStore
    from recertia.jobs.workers import curator_active_set_and_dedup
    from recertia.trajectory.store import TrajectoryStore

    eval_store = EvalStore(job.runs_root / "evals.db")
    try:
        return _run(
            job,
            "curator",
            lambda: curator_active_set_and_dedup(
                job.store,
                trajectory_store=TrajectoryStore(job.runs_root / "trajectories"),
                eval_store=eval_store,
                proposals_path=job.runs_root / "proposals.jsonl",
            ),
        )
    finally:
        eval_store.close()


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
        return _run(
            job,
            "fail_cluster_author",
            lambda: practice_from_fail_clusters(eligible, curriculum_dir=curriculum),
        )
    reasons = explicit if explicit else load_one_off_reasons(job.runs_root / "one_off_log.jsonl")
    if not reasons:
        reasons = ["unsolved one-off cluster"]
    return _run(
        job,
        "practice",
        lambda: practice_from_one_offs(reasons, curriculum_dir=curriculum),
    )


def _run_recertify(job: JobDispatch) -> Any:
    from recertia.evals.store import EvalStore
    from recertia.jobs.workers import recertify_with_revokes

    eval_store = EvalStore(job.runs_root / "evals.db")
    try:
        return _run(
            job,
            "recertify",
            lambda: recertify_with_revokes(
                job.store,
                lineage_index=job.lineage.index,
                revoke_queue=job.lineage.queue,
                max_writes=job.runner.quota.max_status_writes_per_tick,
                tool_upgraded=job.tool_upgraded,
                eval_store=eval_store,
            ),
        )
    finally:
        eval_store.close()


def _run_shadow(job: JobDispatch) -> Any:
    from recertia.jobs.workers import schedule_shadow_evaluations

    return _run(job, "shadow", lambda: schedule_shadow_evaluations(job.store))


def _run_parallelise(job: JobDispatch) -> Any:
    from recertia.jobs.workers import propose_parallelise

    skill_id = job.skill_id
    if not skill_id:
        raise ConfigError("skill_id required")
    return _run(
        job,
        "parallelise",
        lambda: propose_parallelise(
            skill_id,
            job.skill_version,
            fake_edge_failures=job.fake_edge_failures or None,
        ),
    )


def _run_serialise(job: JobDispatch) -> Any:
    from recertia.jobs.workers import propose_serialise

    skill_id = job.skill_id
    if not skill_id:
        raise ConfigError("skill_id required")
    return _run(
        job,
        "serialise",
        lambda: propose_serialise(
            skill_id,
            job.skill_version,
            merge_conflict_count=job.merge_conflicts or None,
        ),
    )


def _run_correction(job: JobDispatch) -> Any:
    from recertia.jobs.workers import (
        correction_miner_from_reviewer_edits,
        load_reviewer_edits,
    )

    edits = load_reviewer_edits(job.edits_log or job.runs_root / "reviewer_edits.jsonl")
    return _run(
        job,
        "correction",
        lambda: correction_miner_from_reviewer_edits(edits),
    )


def _run_fail_cluster(job: JobDispatch) -> Any:
    from recertia.jobs.workers import practice_from_fail_clusters
    from recertia.memory.episodic import EpisodicStore

    episodic = EpisodicStore(job.runs_root / "episodic")
    eligible = (
        episodic.clusters.eligible() if job.policy.improvement.fail_cluster_curriculum else []
    )
    curriculum = None if job.dry_run else job.runs_root / "practice-curriculum"
    return _run(
        job,
        "fail_cluster_author",
        lambda: practice_from_fail_clusters(eligible, curriculum_dir=curriculum),
    )


def _run_hex(job: JobDispatch) -> Any:
    from recertia.jobs.workers import propose_hex_search

    return _run(job, "practice_hex", propose_hex_search)


def _run_compress(job: JobDispatch) -> Any:
    from recertia.jobs.workers import propose_compress

    return _run(job, "compress", propose_compress)


_SERVICE: JobService | None = None


def _default_service() -> JobService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = JobService(pdp=None)
    return _SERVICE
