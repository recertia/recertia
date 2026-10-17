"""CLI: issue, inspect, and revoke durable API keys."""

from __future__ import annotations

import json
from pathlib import Path

import typer

keys_app = typer.Typer(help="Issue, inspect, and revoke durable API keys.")


def register_keys_commands(app: typer.Typer) -> None:
    app.add_typer(keys_app, name="keys")


@keys_app.command("issue")
def keys_issue(
    tenant_id: str = typer.Option(..., "--tenant"),
    scopes: str = typer.Option(..., "--scopes", help="Comma-separated scopes."),
    actor: str = typer.Option(..., "--actor"),
    db: Path = typer.Option(Path(".recertia/api_keys.sqlite"), "--db"),
) -> None:
    """Issue a key; the plaintext secret is shown exactly once."""

    from recertia.persistence.sqlite.api_keys import ApiKeyStore

    issued = ApiKeyStore(db).issue(
        tenant_id=tenant_id,
        scopes={scope.strip() for scope in scopes.split(",") if scope.strip()},
        actor=actor,
    )
    typer.echo(
        f"key_id={issued.key_id} tenant={issued.tenant_id} "
        f"scopes={','.join(sorted(issued.scopes))}"
    )
    typer.echo(f"secret={issued.secret}")


@keys_app.command("revoke")
def keys_revoke(
    key_id: str = typer.Argument(...),
    actor: str = typer.Option(..., "--actor"),
    db: Path = typer.Option(Path(".recertia/api_keys.sqlite"), "--db"),
) -> None:
    """Revoke an API key and write an audit event."""

    from recertia.persistence.sqlite.api_keys import ApiKeyStore

    if not ApiKeyStore(db).revoke(key_id, actor=actor):
        raise typer.Exit(code=1)
    typer.echo(f"revoked={key_id}")


@keys_app.command("list")
def keys_list(db: Path = typer.Option(Path(".recertia/api_keys.sqlite"), "--db")) -> None:
    """List key metadata without ever exposing a key secret or hash."""

    from recertia.persistence.sqlite.api_keys import ApiKeyStore

    for record in ApiKeyStore(db).list_keys():
        typer.echo(json.dumps(record, sort_keys=True))
