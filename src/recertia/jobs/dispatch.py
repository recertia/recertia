"""Single owner for named improvement jobs (CLI + HTTP).

``execute_job`` runs the ladder. It never writes approved, never writes candidates,
and never enqueues the console ProposalStore. CLI ``--submit`` and HTTP
``job_runs`` / proposal inbox stay in the adapters.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from contracts.policy import Policy
from contracts.skill import SkillVersion
from recertia.jobs import JobBudget, JobResult, Proposal, build_job_runner
from recertia.jobs.enablement import attach_enablement
from recertia.jobs.workers import (
    correction_miner_from_reviewer_edits,
    curator_active_set_and_dedup,
    enqueue_mined_candidate,
    load_one_off_reasons,
    load_reviewer_edits,
    mine_from_arxiv,
    mine_from_repo_hints,
    practice_from_fail_clusters,
    practice_from_one_offs,
    propose_compress,
    propose_hex_search,
    propose_parallelise,
    propose_serialise,
    recertify_with_revokes,
    schedule_shadow_evaluations,
)
from recertia.memory.procedural.store import SkillStore
from recertia.policy_load import load_policy
from recertia.trajectory.store import TrajectoryStore

_ALIASES = {
    "miner": "mine",
    "curate": "curator",
    "parallelize": "parallelise",
    "serialize": "serialise",
    "correction_miner": "correction",
    "hex": "practice_hex",
}

KNOWN_JOBS = frozenset(
    {
        "mine",
        "curator",
        "practice",
        "recertify",
        "shadow",
        "parallelise",
        "serialise",
        "correction",
        "practice_hex",
        "compress",
    }
)

DEFAULT_MINE_HINTS = ("README.md chore hints",)


class UnknownJob(ValueError):
    """Job name is not on the improvement-plane ladder."""


class JobDispatchError(ValueError):
    """Job was recognized but required fields were missing."""


@dataclass(frozen=True)
class JobRequest:
    name: str
    dry_run: bool = False
    max_proposals: int = 10
    max_tokens: int = 0
    task_class: str | None = None
    hint: list[str] | None = None
    arxiv_id: list[str] | None = None
    arxiv_query: str | None = None
    arxiv_max: int = 5
    with_pdf: bool = False
    pdf_sandbox: bool = False
    one_off: list[str] | None = None
    tool_upgraded: str | None = None
    skill_id: str | None = None
    skill_version: int = 1
    fake_edge_failures: int = 0
    merge_conflicts: int = 0
    edits_log: Path | None = None
    arxiv_client: object | None = None


def canonical_job_name(name: str) -> str:
    raw = name.strip().lower()
    return _ALIASES.get(raw, raw)


def execute_job(
    request: JobRequest,
    *,
    store: SkillStore,
    runs_root: Path,
    skills_root: Path,
    policy: Policy | None = None,
) -> JobResult:
    """Run the named job. Never writes approved or candidates."""

    from recertia.evals.store import EvalStore

    name = canonical_job_name(request.name)
    if name not in KNOWN_JOBS:
        raise UnknownJob(
            "unknown job "
            f"{request.name!r}; expected mine|curator|practice|recertify|shadow|"
            "parallelise|serialise|correction|hex|compress"
        )

    loaded = policy or load_policy()
    runner = build_job_runner(store, runs_root=runs_root / "jobs", policy=loaded)
    attach_enablement(
        runner,
        eval_db=runs_root / "evals.db",
        skills_root=skills_root,
    )
    budget = JobBudget(max_proposals=request.max_proposals, max_tokens=request.max_tokens)
    traj_store = TrajectoryStore(runs_root / "trajectories")

    def _run(job_name: str, fn, *, budget=budget) -> JobResult:
        return runner.run(job_name, fn, budget=budget, task_class=request.task_class)

    if name == "mine":
        return _execute_mine(request, store=store, runs_root=runs_root, run=_run)
    if name == "curator":
        eval_store = EvalStore(runs_root / "evals.db")
        try:
            return _run(
                "curator",
                lambda: curator_active_set_and_dedup(
                    store,
                    trajectory_store=traj_store,
                    eval_store=eval_store,
                    proposals_path=runs_root / "proposals.jsonl",
                ),
            )
        finally:
            eval_store.close()
    if name == "practice":
        return _execute_practice(request, runs_root=runs_root, policy=loaded, run=_run)
    if name == "recertify":
        eval_store = EvalStore(runs_root / "evals.db")
        try:
            return _run(
                "recertify",
                lambda: recertify_with_revokes(
                    store,
                    lineage_index=store.lineage_index,
                    revoke_queue=store.revoke_queue,
                    max_writes=runner.quota.max_status_writes_per_tick,
                    tool_upgraded=request.tool_upgraded,
                    eval_store=eval_store,
                ),
            )
        finally:
            eval_store.close()
    if name == "shadow":
        return _run("shadow", lambda: schedule_shadow_evaluations(store))
    if name == "parallelise":
        if not request.skill_id:
            raise JobDispatchError("skill_id is required for parallelise")
        return _run(
            "parallelise",
            lambda: propose_parallelise(
                request.skill_id,
                request.skill_version,
                fake_edge_failures=request.fake_edge_failures or None,
            ),
        )
    if name == "serialise":
        if not request.skill_id:
            raise JobDispatchError("skill_id is required for serialise")
        return _run(
            "serialise",
            lambda: propose_serialise(
                request.skill_id,
                request.skill_version,
                merge_conflict_count=request.merge_conflicts or None,
            ),
        )
    if name == "correction":
        edits = load_reviewer_edits(
            request.edits_log or runs_root / "reviewer_edits.jsonl"
        )
        return _run(
            "correction",
            lambda: correction_miner_from_reviewer_edits(edits),
        )
    if name == "practice_hex":
        return _run("practice_hex", propose_hex_search)
    return _run("compress", propose_compress)


def persist_mine_candidates(
    store: SkillStore,
    result: JobResult,
    *,
    distill_paper: bool = False,
    facts_root: Path | None = None,
) -> list[tuple[SkillVersion, list]]:
    """CLI ``--submit`` only. Never approved."""

    if distill_paper:
        from recertia.jobs.paper_pipeline import submit_paper_proposals
        from recertia.memory.semantic import FactStore

        fact_store = FactStore(facts_root or Path(".recertia/facts"))
        return submit_paper_proposals(
            store,
            result.proposals,
            fact_store=fact_store,
            distill=True,
        )
    written: list[tuple[SkillVersion, list]] = []
    for proposal in result.proposals:
        written.append((enqueue_mined_candidate(store, proposal), []))
    return written


def _use_arxiv(request: JobRequest) -> bool:
    return bool(request.arxiv_id) or bool(
        request.arxiv_query and request.arxiv_query.strip()
    )


def _execute_mine(request: JobRequest, *, store: SkillStore, runs_root: Path, run) -> JobResult:
    if _use_arxiv(request):

        def _mine_arxiv() -> list[Proposal]:
            return mine_from_arxiv(
                store,
                arxiv_ids=list(request.arxiv_id or []),
                query=request.arxiv_query,
                max_results=request.arxiv_max,
                client=request.arxiv_client,
            )

        result = run("mine", _mine_arxiv)
        if request.with_pdf and result.proposals:
            from recertia.jobs.paper_pipeline import enrich_proposals_with_pdf

            result.proposals = enrich_proposals_with_pdf(
                result.proposals,
                dest_dir=runs_root / "arxiv-pdfs",
                use_sandbox=request.pdf_sandbox,
                sandbox_workdir=runs_root / "arxiv-pdf-sandbox",
            )
        return result
    hints = list(request.hint or list(DEFAULT_MINE_HINTS))
    return run("mine", lambda: mine_from_repo_hints(store, hints=hints))


def _execute_practice(request: JobRequest, *, runs_root: Path, policy: Policy, run) -> JobResult:
    from recertia.memory.episodic import EpisodicStore

    explicit = list(request.one_off) if request.one_off else None
    episodic = EpisodicStore(runs_root / "episodic")
    eligible = (
        episodic.clusters.eligible()
        if policy.improvement.fail_cluster_curriculum and not explicit
        else []
    )
    curriculum = None if request.dry_run else runs_root / "practice-curriculum"
    if eligible:
        return run(
            "fail_cluster_author",
            lambda: practice_from_fail_clusters(eligible, curriculum_dir=curriculum),
        )
    reasons = explicit if explicit else load_one_off_reasons(runs_root / "one_off_log.jsonl")
    if not reasons:
        reasons = ["unsolved one-off cluster"]
    return run(
        "practice",
        lambda: practice_from_one_offs(reasons, curriculum_dir=curriculum),
    )
