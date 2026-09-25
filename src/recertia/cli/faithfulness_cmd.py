"""CLI: ``recertia faithfulness`` — eval-only condensed-memory interventions."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

faithfulness_app = typer.Typer(help="Faithfulness interventions (eval-only, never production).")


def register_faithfulness_commands(app: typer.Typer) -> None:
    app.add_typer(faithfulness_app, name="faithfulness")


@faithfulness_app.command("run")
def faithfulness_run(
    skill_id: str = typer.Option(..., "--skill-id"),
    version: int = typer.Option(1, "--version"),
    task_class: str = typer.Option("repo-chore", "--task-class"),
    skills_root: Path = typer.Option(Path("skills"), "--skills-root"),
    interventions: str = typer.Option(
        "empty,corrupt,irrelevant,filler", "--interventions"
    ),
    donor_skill_id: Optional[str] = typer.Option(None, "--donor-skill-id"),
    donor_version: int = typer.Option(1, "--donor-version"),
    eval_db: Path = typer.Option(Path(".recertia/evals.db"), "--eval-db"),
    ledger_path: Optional[Path] = typer.Option(None, "--ledger"),
    runs_root: Optional[Path] = typer.Option(None, "--runs-root"),
    golden_root: Path = typer.Option(Path("evals/golden"), "--golden-root"),
    trials: int = typer.Option(
        0,
        "--trials",
        help="Execute N golden fixtures under each intervention (0 = score stored rows only).",
    ),
) -> None:
    """Score tagged trials, optionally execute them, write a ledger tag. Never production."""

    import json

    from contracts.eval import BinomialSample
    from recertia.evals.faithfulness import (
        evaluate_faithfulness,
        event_kinds,
        run_intervened_trials,
        strategy_tag,
    )
    from recertia.evals.interventions import apply_intervention
    from recertia.evals.store import EvalStore
    from recertia.memory.procedural.store import SkillStore
    from recertia.policy_load import load_policy

    policy = load_policy()
    names = [part.strip() for part in interventions.split(",") if part.strip()]
    store = SkillStore(skills_root)
    skill = store.get_version(skill_id, version)
    donor = None
    needs_donor = "irrelevant" in names and (trials > 0 or bool(donor_skill_id))
    if needs_donor:
        if not donor_skill_id:
            raise typer.BadParameter("--donor-skill-id required for irrelevant execution")
        donor = store.get_version(donor_skill_id, donor_version)
    elif "irrelevant" in names and donor_skill_id:
        donor = store.get_version(donor_skill_id, donor_version)

    transformed = {}
    for name in names:
        if name == "irrelevant" and donor is None:
            continue
        transformed[name] = apply_intervention(skill, name, donor=donor)  # type: ignore[arg-type]

    eval_store = EvalStore(eval_db)
    try:
        if trials > 0:
            from recertia.evals.golden import list_goldens_for_task_class

            fixtures = [
                path.name for path in list_goldens_for_task_class(golden_root, task_class)[:trials]
            ]
            if not fixtures:
                raise typer.BadParameter(
                    f"no golden fixtures under {golden_root / task_class} for --trials {trials}"
                )
            for name in names:
                if name == "irrelevant" and donor is None:
                    continue
                run_intervened_trials(
                    skill=skill,
                    intervention=name,  # type: ignore[arg-type]
                    fixture_ids=fixtures,
                    eval_store=eval_store,
                    inner_store=store,
                    donor=donor,
                    runner=_default_runner(
                        runs_root=runs_root or eval_db.parent,
                        golden_root=golden_root,
                        task_class=task_class,
                    ),
                )

        observations = eval_store.list_observations(task_class=task_class)
    finally:
        eval_store.close()

    traj = None
    if runs_root is not None:
        from recertia.trajectory.store import TrajectoryStore

        traj = TrajectoryStore(runs_root / "trajectories")

    def _kinds_for(obs: object) -> list[str]:
        run_id = getattr(obs, "run_id", "")
        if traj is not None:
            events = traj.list_events(run_id)
            if events:
                return event_kinds(events)
        return [getattr(obs, "terminal", None) or "unknown"]

    def _groups(rows: list) -> dict[str, list[str]]:
        grouped: dict[str, list[list[str]]] = {}
        for obs in rows:
            key = getattr(obs, "fixture_id", None) or getattr(obs, "run_id")
            grouped.setdefault(str(key), []).append(_kinds_for(obs))
        # first trial per fixture; pairing is by fixture_id
        return {key: series[0] for key, series in grouped.items()}

    baseline_rows = [
        obs
        for obs in observations
        if obs.skill_id == skill_id
        and obs.skill_version == version
        and not (obs.strategy or "").startswith("faithfulness:")
        and not obs.is_eval_fixture
    ]
    baseline = BinomialSample(
        successes=sum(1 for obs in baseline_rows if obs.first_attempt_success),
        trials=len(baseline_rows),
    )
    outcomes = {}
    event_groups: dict[str, dict[str, list[str]]] = {}
    for name in names:
        tag = strategy_tag(name)  # type: ignore[arg-type]
        rows = [obs for obs in observations if obs.strategy == tag]
        outcomes[name] = BinomialSample(
            successes=sum(1 for obs in rows if obs.first_attempt_success),
            trials=len(rows),
        )
        event_groups[name] = _groups(rows)

    report = evaluate_faithfulness(
        skill=skill,
        baseline=baseline,
        baseline_event_groups=_groups(baseline_rows),
        outcomes=outcomes,  # type: ignore[arg-type]
        event_groups=event_groups,  # type: ignore[arg-type]
        donor=donor,
        skill_used=bool(baseline_rows),
        min_independent_runs=policy.min_independent_runs,
    )

    payload = report.model_dump(mode="json")
    if report.score is None:
        payload["unavailable"] = "no intervened trials"
    payload["transformed"] = {
        name: {"title": body.title, "step_intents": [s.intent for s in body.steps]}
        for name, body in transformed.items()
    }
    typer.echo(json.dumps(payload, indent=2, default=str))
    if report.score is None:
        typer.echo("score=unavailable (no intervened trials)", err=True)

    if ledger_path is not None:
        from recertia.ledger import HashChainLedger

        ledger = HashChainLedger(ledger_path)
        ledger.append(
            actor="recertia-faithfulness",
            action="faithfulness_report",
            target=f"{skill_id}@v{version}",
            evidence={
                "score": report.score,
                "scored_arms": report.scored_arms,
                "interventions": names,
                "tagged": True,
                "production_path": False,
            },
            at=datetime.now(timezone.utc),
        )


def _default_runner(
    *,
    runs_root: Path,
    golden_root: Path,
    task_class: str,
):
    """Build a GraphOrchestrator runner. Does not import recertia.nodes (T3)."""

    def runner(run_id: str, fixture_id: str, *, overlay, bundle_hook):
        from contracts.budget import Budget
        from contracts.criteria import TaskCriterion
        from contracts.run import RunManifest, Task
        from recertia.graph.engine import GraphOrchestrator
        from recertia.retrieval.index import SkillIndex
        from recertia.retrieval.pipeline import Retriever

        index = SkillIndex(runs_root / "faithfulness-index.db")
        try:
            if hasattr(overlay, "iter_loaded"):
                index.rebuild(overlay.iter_loaded())  # type: ignore[arg-type]
            retriever = Retriever(index, bundle_hook=bundle_hook)
            orch = GraphOrchestrator(
                runs_root / "faithfulness-runs",
                store=overlay,  # type: ignore[arg-type]
                retriever=retriever,
            )
            fixture_dir = golden_root / task_class / fixture_id
            request = fixture_id
            if (fixture_dir / "goal.json").exists():
                request = (fixture_dir / "goal.json").read_text(encoding="utf-8")[:200]
            elif (fixture_dir / "task.json").exists():
                import json as _json

                spec = _json.loads((fixture_dir / "task.json").read_text(encoding="utf-8"))
                request = str(spec.get("request") or fixture_id)
            criterion = TaskCriterion(
                id="faithfulness-gate",
                kind="command",
                run="true",
                source="caller",
                weight=1.0,
            )
            workdir = runs_root / "faithfulness-ws" / run_id
            workdir.mkdir(parents=True, exist_ok=True)
            try:
                return orch.start(
                    run_id,
                    Task(
                        task_id=fixture_id,
                        request=request,
                        task_class=task_class,
                        submitted_at=datetime.now(timezone.utc),
                        is_eval_fixture=True,
                    ),
                    [criterion],
                    budget=Budget(max_attempts=1),
                    workdir=workdir,
                    manifest=RunManifest(
                        index_snapshot_id="faithfulness",
                        criteria_hash="faithfulness",
                    ),
                )
            finally:
                orch.close()
                index.close()
        except Exception:
            index.close()
            raise

    return runner
