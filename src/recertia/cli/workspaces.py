"""CLI: ``recertia workspaces register|list|disable``."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from recertia.paths import HostRootError
from recertia.workspaces.registry import WorkspaceRegistry

workspaces_app = typer.Typer(help="Registered host workspaces (Pilot / API bind allowlist).")


def register_workspaces_commands(app: typer.Typer) -> None:
    app.add_typer(workspaces_app, name="workspaces")


@workspaces_app.command("register")
def workspaces_register(
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
    from recertia.policy_load import load_policy
    from recertia.ports.identity import Principal
    from recertia.settings import load_settings

    principal = Principal.local_operator()
    pdp = PolicyDecisionPoint.open(load_settings(), load_policy(), surface="cli")
    if not pdp.may_admin_host(principal):
        typer.echo("forbidden: registering a host root requires platform_admin", err=True)
        raise typer.Exit(code=3)
    registry = WorkspaceRegistry(runs_root / "workspaces_registry.sqlite")
    try:
        ws = registry.register(
            tenant_id=tenant,
            workspace_id=workspace_id,
            display_name=display_name or workspace_id,
            host_root=str(host_root),
            created_by=actor,
            notes=notes,
        )
    except LookupError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except (HostRootError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    finally:
        registry.close()
    typer.echo(
        f"workspace_id={ws.workspace_id} tenant={ws.tenant_id} host_root={ws.host_root}"
    )


@workspaces_app.command("list")
def workspaces_list(
    tenant: str = typer.Option("default", "--tenant"),
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root"),
) -> None:
    registry = WorkspaceRegistry(runs_root / "workspaces_registry.sqlite")
    try:
        items = registry.list(tenant_id=tenant)
    finally:
        registry.close()
    if not items:
        typer.echo("(none)")
        return
    for ws in items:
        flag = "on" if ws.enabled else "off"
        typer.echo(
            f"{ws.workspace_id}\t{flag}\t{ws.display_name}\t{ws.host_root}"
        )


@workspaces_app.command("disable")
def workspaces_disable(
    workspace_id: str = typer.Argument(...),
    tenant: str = typer.Option("default", "--tenant"),
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root"),
) -> None:
    registry = WorkspaceRegistry(runs_root / "workspaces_registry.sqlite")
    try:
        ws = registry.set_enabled(workspace_id, tenant_id=tenant, enabled=False)
    finally:
        registry.close()
    if ws is None:
        typer.echo("workspace not found", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"workspace_id={ws.workspace_id} enabled=false")
