"""CLI: issue, inspect, and revoke durable API keys."""

from __future__ import annotations

import json
from pathlib import Path

import typer

keys_app = typer.Typer(help="Issue, inspect, and revoke durable API keys.")


def register_keys_commands(app: typer.Typer) -> None:
    app.add_typer(keys_app, name="keys")


def _principal(ctx: typer.Context):
    from recertia.ports.identity import Principal

    return (
        ctx.obj["principal"]
        if isinstance(ctx.obj, dict) and ctx.obj.get("principal") is not None
        else Principal.local_operator()
    )


@keys_app.command("issue")
def keys_issue(
    ctx: typer.Context,
    tenant_id: str = typer.Option(..., "--tenant"),
    scopes: str = typer.Option(..., "--scopes", help="Comma-separated scopes."),
    actor: str = typer.Option(..., "--actor"),
    db: Path = typer.Option(Path(".recertia/api_keys.sqlite"), "--db"),
) -> None:
    """Issue a key; the plaintext secret is shown exactly once."""

    from recertia.api.auth import ApiKeyStore
    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.keys import KeyService
    from recertia.errors import RecertiaError

    try:
        issued = KeyService(pdp=PolicyDecisionPoint.load(surface="cli")).issue(
            _principal(ctx),
            store=ApiKeyStore(db),
            tenant_id=tenant_id,
            scopes={scope.strip() for scope in scopes.split(",") if scope.strip()},
            actor=actor,
        )
    except RecertiaError as exc:
        typer.echo(f"{exc.code}: {exc.message}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc
    typer.echo(
        f"key_id={issued.key_id} tenant={issued.tenant_id} "
        f"scopes={','.join(sorted(issued.scopes))}"
    )
    typer.echo(f"secret={issued.secret}")


@keys_app.command("revoke")
def keys_revoke(
    ctx: typer.Context,
    key_id: str = typer.Argument(...),
    actor: str = typer.Option(..., "--actor"),
    db: Path = typer.Option(Path(".recertia/api_keys.sqlite"), "--db"),
) -> None:
    """Revoke an API key and write an audit event."""

    from recertia.api.auth import ApiKeyStore
    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.keys import KeyService
    from recertia.errors import RecertiaError

    try:
        KeyService(pdp=PolicyDecisionPoint.load(surface="cli")).revoke(
            _principal(ctx), store=ApiKeyStore(db), key_id=key_id, actor=actor
        )
    except RecertiaError as exc:
        raise typer.Exit(code=exc.exit_code) from exc
    typer.echo(f"revoked={key_id}")


@keys_app.command("list")
def keys_list(
    ctx: typer.Context,
    db: Path = typer.Option(Path(".recertia/api_keys.sqlite"), "--db"),
) -> None:
    """List key metadata without ever exposing a key secret or hash."""

    from recertia.api.auth import ApiKeyStore
    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.keys import KeyService
    from recertia.errors import RecertiaError

    try:
        records = KeyService(pdp=PolicyDecisionPoint.load(surface="cli")).list(
            _principal(ctx), store=ApiKeyStore(db)
        )
    except RecertiaError as exc:
        typer.echo(f"{exc.code}: {exc.message}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc
    for record in records:
        typer.echo(json.dumps(record, sort_keys=True))
