"""CLI: emit AgentSysBench six-property snapshot from in-process telemetry or JSONL."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer


def register_systems_commands(app: typer.Typer) -> None:
    app.command("systems")(systems_cmd)


def systems_cmd(
    ctx: typer.Context,
    spans: Optional[Path] = typer.Option(None, "--spans", help="Telemetry JSONL to fold."),
    output: Optional[Path] = typer.Option(None, "--output"),
    brief: bool = typer.Option(False, "--brief", help="Stuck jobs, lift-by-class, redundancy."),
) -> None:
    """Print the six-property snapshot. Does not claim lift or a 4.6× memory cut."""

    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.ops_service import OpsService
    from recertia.errors import RecertiaError
    from recertia.ports.identity import Principal

    principal = (
        ctx.obj["principal"]
        if isinstance(ctx.obj, dict) and ctx.obj.get("principal") is not None
        else Principal.local_operator()
    )
    try:
        payload = OpsService(pdp=PolicyDecisionPoint.load(surface="cli")).systems(
            principal, spans=spans, brief=brief
        )
    except RecertiaError as exc:
        from recertia.cli.exit import raise_exit

        raise_exit(exc)
    text = json.dumps(payload, indent=2)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
    typer.echo(text)
