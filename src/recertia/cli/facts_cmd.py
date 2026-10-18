"""CLI: list semantic facts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

facts_app = typer.Typer(help="Semantic-plane facts (read-only).")


def register_facts_commands(app: typer.Typer) -> None:
    app.add_typer(facts_app, name="facts")


@facts_app.command("list")
def facts_list(
    ctx: typer.Context,
    scope: Optional[str] = typer.Option(None, "--scope", help="run|project|org|global"),
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root"),
    tenant: str = typer.Option("default", "--tenant"),
    facts_root: Optional[Path] = typer.Option(None, "--facts-root"),
) -> None:
    """List facts, optionally filtered by scope."""

    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.library import LibraryService
    from recertia.errors import RecertiaError
    from recertia.memory.semantic import FactStore
    from recertia.ports.identity import Principal

    principal = (
        ctx.obj["principal"]
        if isinstance(ctx.obj, dict) and ctx.obj.get("principal") is not None
        else Principal.local_operator()
    )
    root = facts_root if facts_root is not None else runs_root / "runs" / tenant / "facts"
    store = FactStore(root)
    try:
        items = LibraryService(pdp=PolicyDecisionPoint.load(surface="cli")).list_facts(
            principal, store=store, scope=scope
        )
    except RecertiaError as exc:
        from recertia.cli.exit import raise_exit

        raise_exit(exc)
    typer.echo(json.dumps({"items": [f.model_dump(mode="json") for f in items]}, indent=2))
