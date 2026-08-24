"""CLI: run improvement-plane jobs (mine / curate / practice / recertify / …)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

jobs_app = typer.Typer(help="Improvement-plane jobs (proposals only; never write approved).")


def register_jobs_commands(app: typer.Typer) -> None:
    app.add_typer(jobs_app, name="jobs")


@jobs_app.command("run")
def jobs_run(
    job: str = typer.Argument(
        ...,
        help=(
            "Job name: mine | curator | practice | recertify | shadow | "
            "parallelise | serialise | correction | hex | compress"
        ),
    ),
    skills_root: Path = typer.Option(Path("skills"), "--skills-root"),
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print proposals; do not persist."),
    max_proposals: int = typer.Option(10, "--max-proposals"),
    hint: Optional[list[str]] = typer.Option(
        None, "--hint", help="Mine job: human-artifact hint (repeatable)."
    ),
    arxiv_id: Optional[list[str]] = typer.Option(
        None,
        "--arxiv-id",
        help="Mine job: arXiv id (e.g. 2605.22148). Repeatable. Fetches via export.arxiv.org.",
    ),
    arxiv_query: Optional[str] = typer.Option(
        None,
        "--arxiv-query",
        help="Mine job: arXiv search_query (e.g. 'ti:\"self-evolving agents\"').",
    ),
    arxiv_max: int = typer.Option(
        5, "--arxiv-max", help="Mine job: max_results for --arxiv-query (1–50)."
    ),
    one_off: Optional[list[str]] = typer.Option(
        None, "--one-off", help="Practice job: one-off cluster reason (repeatable)."
    ),
    tool_upgraded: Optional[str] = typer.Option(
        None, "--tool-upgraded", help="Recertify job: tool name that upgraded."
    ),
    skill_id: Optional[str] = typer.Option(
        None, "--skill-id", help="parallelise/serialise: target skill id."
    ),
    skill_version: int = typer.Option(1, "--skill-version", help="parallelise/serialise version."),
    fake_edge_failures: int = typer.Option(
        0, "--fake-edge-failures", help="parallelise: explicit failure count."
    ),
    merge_conflicts: int = typer.Option(
        0, "--merge-conflicts", help="serialise: explicit merge conflict/gap count."
    ),
    edits_log: Optional[Path] = typer.Option(
        None, "--edits-log", help="correction: JSONL of reviewer edits."
    ),
    submit: bool = typer.Option(
        False, "--submit", help="Persist mined drafts as candidates (mine only)."
    ),
    task_class: Optional[str] = typer.Option(
        None,
        "--task-class",
        help="Quota class for computer-use practice share (ADR-0019, snake_case).",
    ),
    max_tokens: int = typer.Option(
        0, "--max-tokens", help="JobQuota tokens to admit/charge (0 = no charge)."
    ),
) -> None:
    """Run an offline improvement job under a proposal budget."""

    from recertia.evals.store import EvalStore
    from recertia.jobs import JobBudget, build_job_runner
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
    from recertia.memory.episodic import EpisodicStore
    from recertia.memory.procedural.lineage import LineageServices
    from recertia.memory.procedural.store import SkillStore
    from recertia.policy_load import load_policy
    from recertia.trajectory.store import TrajectoryStore

    policy = load_policy()
    lineage = LineageServices.open(runs_root / "lineage")
    store = SkillStore(
        skills_root,
        lineage_index=lineage.index,
        revoke_queue=lineage.queue,
    )
    runner = build_job_runner(store, runs_root=runs_root / "jobs", policy=policy)
    attach_enablement(
        runner,
        eval_db=runs_root / "evals.db",
        skills_root=skills_root,
    )
    budget = JobBudget(max_proposals=max_proposals, max_tokens=max_tokens)
    name = job.strip().lower()
    traj_store = TrajectoryStore(runs_root / "trajectories")

    def _run(job_name: str, fn, *, budget=budget):
        return runner.run(job_name, fn, budget=budget, task_class=task_class)


    if name in {"mine", "miner"}:
        use_arxiv = bool(arxiv_id) or bool(arxiv_query and arxiv_query.strip())
        if use_arxiv:

            def _mine_arxiv() -> list:
                return mine_from_arxiv(
                    store,
                    arxiv_ids=list(arxiv_id or []),
                    query=arxiv_query,
                    max_results=arxiv_max,
                )

            result = _run("mine", _mine_arxiv, budget=budget)
        else:
            hints = list(hint or ["README.md chore hints"])
            result = _run(
                "mine", lambda: mine_from_repo_hints(store, hints=hints), budget=budget
            )
        if submit and not dry_run:
            for proposal in result.proposals:
                draft = enqueue_mined_candidate(store, proposal)
                typer.echo(f"candidate {draft.skill_id}@v{draft.version}")
    elif name in {"curator", "curate"}:
        eval_store = EvalStore(runs_root / "evals.db")
        try:
            result = _run(
                "curator",
                lambda: curator_active_set_and_dedup(
                    store,
                    trajectory_store=traj_store,
                    eval_store=eval_store,
                    proposals_path=runs_root / "proposals.jsonl",
                ),
                budget=budget,
            )
        finally:
            eval_store.close()
    elif name == "practice":
        explicit = list(one_off) if one_off else None
        episodic = EpisodicStore(runs_root / "episodic")
        eligible = (
            episodic.clusters.eligible()
            if policy.improvement.fail_cluster_curriculum and not explicit
            else []
        )
        if eligible:
            curriculum = None if dry_run else runs_root / "practice-curriculum"
            result = _run(
                "fail_cluster_author",
                lambda: practice_from_fail_clusters(eligible, curriculum_dir=curriculum),
                budget=budget,
            )
        else:
            reasons = explicit if explicit else load_one_off_reasons(runs_root / "one_off_log.jsonl")
            if not reasons:
                reasons = ["unsolved one-off cluster"]
            curriculum = None if dry_run else runs_root / "practice-curriculum"
            result = _run(
                "practice",
                lambda: practice_from_one_offs(reasons, curriculum_dir=curriculum),
                budget=budget,
            )
    elif name == "recertify":
        eval_store = EvalStore(runs_root / "evals.db")
        try:
            result = _run(
                "recertify",
                lambda: recertify_with_revokes(
                    store,
                    lineage_index=lineage.index,
                    revoke_queue=lineage.queue,
                    max_writes=runner.quota.max_status_writes_per_tick,
                    tool_upgraded=tool_upgraded,
                    eval_store=eval_store,
                ),
                budget=budget,
            )
        finally:
            eval_store.close()
    elif name == "shadow":
        result = _run(
            "shadow",
            lambda: schedule_shadow_evaluations(store),
            budget=budget,
        )
    elif name in {"parallelise", "parallelize"}:
        if not skill_id:
            typer.echo("--skill-id is required for parallelise", err=True)
            raise typer.Exit(code=2)
        result = _run(
            "parallelise",
            lambda: propose_parallelise(
                skill_id, skill_version, fake_edge_failures=fake_edge_failures or None
            ),
            budget=budget,
        )
    elif name in {"serialise", "serialize"}:
        if not skill_id:
            typer.echo("--skill-id is required for serialise", err=True)
            raise typer.Exit(code=2)
        result = _run(
            "serialise",
            lambda: propose_serialise(
                skill_id, skill_version, merge_conflict_count=merge_conflicts or None
            ),
            budget=budget,
        )
    elif name in {"correction", "correction_miner"}:
        edits = load_reviewer_edits(edits_log or runs_root / "reviewer_edits.jsonl")
        result = _run(
            "correction",
            lambda: correction_miner_from_reviewer_edits(edits),
            budget=budget,
        )
    elif name in {"hex", "practice_hex"}:
        result = _run("practice_hex", propose_hex_search, budget=budget)
    elif name == "compress":
        result = _run("compress", propose_compress, budget=budget)
    else:
        typer.echo(
            "unknown job "
            f"{job!r}; expected mine|curator|practice|recertify|shadow|"
            "parallelise|serialise|correction|hex|compress",
            err=True,
        )
        raise typer.Exit(code=2)

    payload = {
        "job": result.job,
        "skipped": result.skipped,
        "proposals": [
            {
                "kind": p.kind,
                "skill_id": p.skill_id,
                "version": p.version,
                "rationale": p.rationale,
                "payload": p.payload,
            }
            for p in result.proposals
        ],
        "dry_run": dry_run,
    }
    typer.echo(json.dumps(payload, indent=2))
