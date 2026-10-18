"""CLI twins for distill-review (alias of the proposal queue)."""

from __future__ import annotations

import json
from pathlib import Path

import typer

review_app = typer.Typer(help="Distill-review queue (alias of proposals until volume splits).")


def register_review_commands(app: typer.Typer) -> None:
    app.add_typer(review_app, name="review")


def _principal(ctx: typer.Context):
    from recertia.ports.identity import Principal

    return (
        ctx.obj["principal"]
        if isinstance(ctx.obj, dict) and ctx.obj.get("principal") is not None
        else Principal.local_operator()
    )


@review_app.command("queue")
def review_queue(
    ctx: typer.Context,
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root"),
    tenant: str = typer.Option("default", "--tenant"),
    status: str = typer.Option("pending", "--status"),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """List pending distill-review decisions (proposal alias)."""

    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.library import LibraryService
    from recertia.errors import RecertiaError
    from recertia.proposals.store import ProposalStore

    store = ProposalStore(runs_root / "proposals.sqlite")
    try:
        payload = LibraryService(pdp=PolicyDecisionPoint.load(surface="cli")).list_reviews(
            _principal(ctx), store=store, tenant_id=tenant, status=status, limit=limit
        )
    except RecertiaError as exc:
        from recertia.cli.exit import raise_exit

        raise_exit(exc)
    finally:
        store.close()
    typer.echo(json.dumps(payload, indent=2))


@review_app.command("approve")
def review_approve(
    ctx: typer.Context,
    decision_id: str = typer.Argument(...),
    note: str = typer.Option("", "--note"),
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root"),
    tenant: str = typer.Option("default", "--tenant"),
    actor: str = typer.Option("cli", "--actor"),
) -> None:
    """Record a human approve on a proposal. Does not write lifecycle=approved."""

    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.library import LibraryService
    from recertia.errors import NotFound, RecertiaError
    from recertia.ledger import HashChainLedger
    from recertia.proposals.store import ProposalStore

    store = ProposalStore(runs_root / "proposals.sqlite")
    ledger = HashChainLedger(runs_root / "runs" / tenant / "ledger.jsonl")
    try:
        updated = LibraryService(pdp=PolicyDecisionPoint.load(surface="cli")).decide_review(
            _principal(ctx),
            store=store,
            decision_id=decision_id,
            tenant_id=tenant,
            decision="approve",
            actor=actor,
            note=note,
            ledger=ledger,
        )
    except RecertiaError as exc:
        from recertia.cli.exit import raise_exit

        if isinstance(exc, NotFound):
            raise_exit(exc, message="review not found")
        raise_exit(exc, message=str(exc.message))
    finally:
        store.close()
    typer.echo(json.dumps(updated.to_dict(), indent=2))
