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
    ctx: typer.Context,
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
    with_pdf: bool = typer.Option(
        False,
        "--with-pdf",
        help="Mine job: download PDF on host; extract text if pypdf is installed.",
    ),
    pdf_sandbox: bool = typer.Option(
        False,
        "--pdf-sandbox",
        help="Mine job: run PDF text extract inside the configured execution backend.",
    ),
    distill_paper: bool = typer.Option(
        False,
        "--distill-paper",
        help="Mine job: on --submit, distill pitfall skill + arxiv-keyed facts.",
    ),
    facts_root: Path = typer.Option(
        Path(".recertia/facts"),
        "--facts-root",
        help="Mine job: FactStore root when --distill-paper --submit.",
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

    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.jobs import JobDispatch, JobService
    from recertia.errors import RecertiaError
    from recertia.jobs import JobBudget, build_job_runner
    from recertia.jobs.enablement import attach_enablement
    from recertia.memory.procedural.lineage import LineageServices
    from recertia.memory.procedural.store import SkillStore
    from recertia.policy_load import load_policy
    from recertia.ports.identity import Principal

    principal = (
        ctx.obj["principal"]
        if isinstance(ctx.obj, dict) and ctx.obj.get("principal") is not None
        else Principal.local_operator()
    )
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
    service = JobService(pdp=PolicyDecisionPoint.load(surface="cli"))
    try:
        result = service.run(
            principal,
            job,
            dispatch=JobDispatch(
                store=store,
                runner=runner,
                budget=budget,
                policy=policy,
                runs_root=runs_root,
                lineage=lineage,
                dry_run=dry_run,
                one_off=list(one_off) if one_off else None,
                tool_upgraded=tool_upgraded,
                hint=list(hint) if hint else None,
                arxiv_id=list(arxiv_id) if arxiv_id else None,
                arxiv_query=arxiv_query,
                arxiv_max=arxiv_max,
                with_pdf=with_pdf,
                pdf_sandbox=pdf_sandbox,
                distill_paper=distill_paper,
                facts_root=facts_root,
                submit=submit,
                skill_id=skill_id,
                skill_version=skill_version,
                fake_edge_failures=fake_edge_failures,
                merge_conflicts=merge_conflicts,
                edits_log=edits_log,
                task_class=task_class,
            ),
        )
    except RecertiaError as exc:
        if exc.code == "not_found":
            typer.echo(
                "unknown job "
                f"{job!r}; expected mine|curator|practice|recertify|shadow|"
                "parallelise|serialise|correction|fail_cluster|hex|compress",
                err=True,
            )
            raise typer.Exit(code=2) from exc
        from recertia.cli.exit import raise_exit

        raise_exit(exc)

    # Strip internal PDF text from JSON echo (can be large).
    safe_proposals = []
    for p in result.proposals:
        pl = dict(p.payload or {})
        pl.pop("_pdf_text", None)
        safe_proposals.append(
            {
                "kind": p.kind,
                "skill_id": p.skill_id,
                "version": p.version,
                "rationale": p.rationale,
                "payload": pl,
            }
        )
    payload = {
        "job": result.job,
        "skipped": result.skipped,
        "proposals": safe_proposals,
        "dry_run": dry_run,
    }
    typer.echo(json.dumps(payload, indent=2))
