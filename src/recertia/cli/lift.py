"""CLI: report causal_lift for a task class (specs §19)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer


def register_lift_commands(app: typer.Typer) -> None:
    app.command("lift")(lift_cmd)


def lift_cmd(
    task_class: str = typer.Option("repo-chore", "--task-class"),
    eval_db: Path = typer.Option(Path(".recertia/evals.db"), "--eval-db"),
    snapshot_id: Optional[str] = typer.Option(None, "--snapshot-id"),
    ledger_path: Optional[Path] = typer.Option(None, "--ledger"),
) -> None:
    """Report causal_lift for a task class (specs §19). Never claims lift when CI includes zero."""

    from datetime import datetime, timezone

    from contracts.eval import BinomialSample
    from recertia.evals.statistics import causal_lift
    from recertia.evals.store import EvalStore
    from recertia.policy_load import load_policy

    policy = load_policy()
    store = EvalStore(eval_db)
    try:
        counts = store.arm_counts(task_class=task_class, snapshot_id=snapshot_id)
        treatment = counts.get("treatment", BinomialSample(successes=0, trials=0))
        control = counts.get("control", BinomialSample(successes=0, trials=0))
        t_rates, c_rates, paired, series_kind = store.variance_inputs(
            task_class=task_class, snapshot_id=snapshot_id
        )
        result = causal_lift(
            treatment,
            control,
            task_class=task_class,
            snapshot_id=snapshot_id,
            min_independent_runs=policy.min_independent_runs,
            treatment_rates=t_rates or None,
            control_rates=c_rates or None,
            paired_lifts=paired,
        )
    finally:
        store.close()

    typer.echo(f"task_class={task_class}")
    typer.echo(f"treatment={treatment.successes}/{treatment.trials}")
    typer.echo(f"control={control.successes}/{control.trials}")
    typer.echo(
        f"independent_runs={result.independent_runs} "
        f"min_independent_runs={result.min_independent_runs}"
    )
    if result.estimate is None:
        typer.echo(f"status={result.render_status()}")
    else:
        typer.echo(f"estimate={result.estimate:.4f}")
        if result.interval is not None:
            typer.echo(
                f"interval=[{result.interval.low:.4f}, {result.interval.high:.4f}] "
                f"level={result.interval.level} method={result.interval.method}"
            )
        typer.echo(f"status={result.render_status()}")
    _echo_variance("treatment", result.treatment_variance, series_kind)
    _echo_variance("control", result.control_variance, series_kind)
    _echo_variance("lift", result.lift_variance, series_kind)
    if result.status == "not_established":
        typer.echo("claim=not established (interval includes zero)")
    elif result.status == "low_run_count":
        typer.echo(
            "claim=not established (independent run count below floor; "
            "never claims established lift)"
        )

    if ledger_path is not None:
        from recertia.ledger import HashChainLedger

        ledger = HashChainLedger(ledger_path)
        ledger.append(
            actor="recertia-lift",
            action="lift_report",
            target=task_class,
            evidence=result.model_dump(mode="json"),
            at=datetime.now(timezone.utc),
        )


def _echo_variance(label: str, variance: object, series_kind: str) -> None:
    from contracts.eval import RunVariance

    if not isinstance(variance, RunVariance) or variance.n_runs < 2:
        return
    if series_kind != "snapshot":
        if variance.std_dev is None:
            return
        typer.echo(
            f"{label}_variance n={variance.n_runs} std_dev={variance.std_dev:.4f} "
            f"(observation-level; gap omitted)"
        )
        return
    typer.echo(
        f"{label}_variance n={variance.n_runs} std_dev={variance.std_dev:.4f} "
        f"best={variance.best_rate:.4f} worst={variance.worst_rate:.4f} "
        f"gap={variance.best_worst_gap:.4f}"
    )
