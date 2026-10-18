"""CLI: list improvement-plane proposals."""

from __future__ import annotations

import json
from pathlib import Path

import typer

proposals_app = typer.Typer(help="Improvement-plane proposal queue.")


def register_proposals_commands(app: typer.Typer) -> None:
    app.add_typer(proposals_app, name="proposals")


@proposals_app.command("queue")
def proposals_queue(
    ctx: typer.Context,
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root"),
    tenant: str = typer.Option("default", "--tenant"),
    status: str = typer.Option("pending", "--status"),
    kind: str | None = typer.Option(None, "--kind"),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """List proposals (pending by default)."""

    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.library import LibraryService
    from recertia.errors import RecertiaError
    from recertia.ports.identity import Principal
    from recertia.proposals.store import ProposalStore

    principal = (
        ctx.obj["principal"]
        if isinstance(ctx.obj, dict) and ctx.obj.get("principal") is not None
        else Principal.local_operator()
    )
    store = ProposalStore(runs_root / "proposals.sqlite")
    try:
        items = LibraryService(pdp=PolicyDecisionPoint.load(surface="cli")).list_proposals(
            principal, store=store, tenant_id=tenant, status=status, kind=kind, limit=limit
        )
    except RecertiaError as exc:
        from recertia.cli.exit import raise_exit

        raise_exit(exc)
    finally:
        store.close()
    typer.echo(json.dumps({"items": [p.to_dict() for p in items]}, indent=2))
