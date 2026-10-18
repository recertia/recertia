"""CLI: planted-failure judge canary (synthetic shell or live verifier)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer


def register_canary_commands(app: typer.Typer) -> None:
    app.command("canary")(canary_cmd)


def canary_cmd(
    ctx: typer.Context,
    live: bool = typer.Option(
        False,
        "--live",
        help="Score fixtures with RECERTIA_VERIFIER_MODEL_ID (does not update a4).",
    ),
    canary_root: Path = typer.Option(
        Path("evals/canary/planted-failure"), "--canary-root"
    ),
    output: Optional[Path] = typer.Option(None, "--output"),
) -> None:
    """Run the planted-failure canary. Default is the local shell verifier."""

    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.evals import EvalService
    from recertia.config import load_model_config
    from recertia.errors import RecertiaError
    from recertia.evals.canary import LiveCanaryError
    from recertia.ports.identity import Principal

    principal = (
        ctx.obj["principal"]
        if isinstance(ctx.obj, dict) and ctx.obj.get("principal") is not None
        else Principal.local_operator()
    )
    cfg = load_model_config()
    if cfg.verifier_model_id and cfg.model_id and cfg.verifier_model_id == cfg.model_id:
        typer.echo(
            "warning: RECERTIA_VERIFIER_MODEL_ID equals RECERTIA_MODEL_ID; "
            "prefer a distinct verifier slug",
            err=True,
        )

    service = EvalService(pdp=PolicyDecisionPoint.load(surface="cli"))
    try:
        report = service.run_canary(
            principal,
            live=live,
            canary_root=canary_root,
            model_version=None if live else cfg.verifier_model_id,
        )
    except RecertiaError as exc:
        typer.echo(f"{exc.code}: {exc.message}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc
    except LiveCanaryError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    payload = {
        "mode": report.mode,
        "trials": report.trials,
        "false_passes": report.false_passes,
        "false_pass_rate": report.false_pass_rate,
        "model_version": report.model_version,
        "attribution": report.attribution,
        "unavailable": report.unavailable,
        "solver_verifier_same_model": report.solver_verifier_same_model,
        "a4_updated": False,
    }
    text = json.dumps(payload, indent=2)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
    typer.echo(text)
    if report.trials < 1 or report.false_passes > 0:
        raise typer.Exit(code=1)
