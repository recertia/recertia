"""CLI: ``recertia workspaces register|list|disable``."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from recertia.workspaces.registry import WorkspaceRegistry

workspaces_app = typer.Typer(help="Registered host workspaces (Pilot / API bind allowlist).")


def register_workspaces_commands(app: typer.Typer) -> None:
    app.add_typer(workspaces_app, name="workspaces")


def _principal(ctx: typer.Context):
    from recertia.ports.identity import Principal

    return (
        ctx.obj["principal"]
        if isinstance(ctx.obj, dict) and ctx.obj.get("principal") is not None
        else Principal.local_operator()
    )


@workspaces_app.command("register")
def workspaces_register(
    ctx: typer.Context,
    workspace_id: str = typer.Option(..., "--id", help="Stable workspace_id slug."),
    host_root: Path = typer.Option(..., "--host-root", help="Absolute host directory."),
    display_name: Optional[str] = typer.Option(None, "--name", help="Display label."),
    tenant: str = typer.Option("default", "--tenant"),
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root"),
    notes: Optional[str] = typer.Option(None, "--notes"),
    actor: str = typer.Option("cli", "--actor"),
) -> None:
    """Register an allowlisted host root for API/Pilot ``workspace_id`` binds."""

    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.workspaces import WorkspaceService
    from recertia.errors import Forbidden, RecertiaError

    registry = WorkspaceRegistry(runs_root / "workspaces_registry.sqlite")
    try:
        ws = WorkspaceService(pdp=PolicyDecisionPoint.load(surface="cli")).register(
            _principal(ctx),
            registry=registry,
            tenant_id=tenant,
            workspace_id=workspace_id,
            display_name=display_name or workspace_id,
            host_root=str(host_root),
            created_by=actor,
            notes=notes,
        )
    except RecertiaError as exc:
        from recertia.cli.exit import raise_exit

        if isinstance(exc, Forbidden):
            raise_exit(
                exc,
                message="forbidden: registering a host root requires platform_admin",
            )
        raise_exit(exc, message=str(exc.message))
    finally:
        registry.close()
    typer.echo(
        f"workspace_id={ws.workspace_id} tenant={ws.tenant_id} host_root={ws.host_root}"
    )


@workspaces_app.command("list")
def workspaces_list(
    ctx: typer.Context,
    tenant: str = typer.Option("default", "--tenant"),
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root"),
) -> None:
    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.workspaces import WorkspaceService
    from recertia.errors import RecertiaError

    registry = WorkspaceRegistry(runs_root / "workspaces_registry.sqlite")
    try:
        items = WorkspaceService(pdp=PolicyDecisionPoint.load(surface="cli")).list(
            _principal(ctx), registry=registry, tenant_id=tenant
        )
    except RecertiaError as exc:
        from recertia.cli.exit import raise_exit

        raise_exit(exc)
    finally:
        registry.close()
    if not items:
        typer.echo("(none)")
        return
    for ws in items:
        flag = "on" if ws.enabled else "off"
        typer.echo(f"{ws.workspace_id}\t{flag}\t{ws.display_name}\t{ws.host_root}")


@workspaces_app.command("disable")
def workspaces_disable(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(...),
    tenant: str = typer.Option("default", "--tenant"),
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root"),
) -> None:
    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.workspaces import WorkspaceService
    from recertia.errors import RecertiaError

    registry = WorkspaceRegistry(runs_root / "workspaces_registry.sqlite")
    try:
        ws = WorkspaceService(pdp=PolicyDecisionPoint.load(surface="cli")).disable(
            _principal(ctx),
            registry=registry,
            tenant_id=tenant,
            workspace_id=workspace_id,
        )
    except RecertiaError as exc:
        from recertia.cli.exit import raise_exit

        raise_exit(exc)
    finally:
        registry.close()
    typer.echo(f"workspace_id={ws.workspace_id} enabled=false")
