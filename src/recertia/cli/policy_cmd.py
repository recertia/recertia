"""CLI: show loaded T2 policy or file a proposal (does not apply)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import typer

policy_app = typer.Typer(
    help="Show loaded T2 policy or file a proposal (does not apply).",
    invoke_without_command=True,
)


def register_policy_commands(app: typer.Typer) -> None:
    app.add_typer(policy_app, name="policy")


def _principal(ctx: typer.Context | None):
    from recertia.ports.identity import Principal

    if ctx is None or not isinstance(ctx.obj, dict) or ctx.obj.get("principal") is None:
        return Principal.local_operator()
    return ctx.obj["principal"]


@policy_app.callback(invoke_without_command=True)
def policy_root(
    ctx: typer.Context,
    path: Optional[Path] = typer.Option(None, "--path", help="Policy JSON; default env/file."),
) -> None:
    if ctx.invoked_subcommand is None:
        policy_show(ctx, path)


@policy_app.command("show")
def policy_show(
    ctx: typer.Context,
    path: Optional[Path] = typer.Option(None, "--path", help="Policy JSON; default env/file."),
) -> None:
    """Print the loaded Policy document (remaining-work RW-SUR)."""

    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.library import LibraryService
    from recertia.errors import RecertiaError

    try:
        payload = LibraryService(pdp=PolicyDecisionPoint.load(surface="cli")).get_policy(
            _principal(ctx), path=path
        )
    except RecertiaError as exc:
        from recertia.cli.exit import raise_exit

        raise_exit(exc)
    typer.echo(json.dumps(payload, indent=2))


def _parse_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def parse_policy_assignment(raw: str) -> dict[str, Any]:
    """Turn ``dotted.path=value`` into a nested dict (opaque T2 diff)."""

    if "=" not in raw:
        raise typer.BadParameter("expected dotted.path=value")
    key, _, value = raw.partition("=")
    key = key.strip()
    if not key:
        raise typer.BadParameter("empty policy key")
    parsed = _parse_value(value.strip())
    parts = [p for p in key.split(".") if p]
    if not parts:
        raise typer.BadParameter("empty policy key")
    out: dict[str, Any] = {}
    cursor: dict[str, Any] = out
    for part in parts[:-1]:
        nxt: dict[str, Any] = {}
        cursor[part] = nxt
        cursor = nxt
    cursor[parts[-1]] = parsed
    return out


@policy_app.command("propose")
def policy_propose(
    ctx: typer.Context,
    assignment: str = typer.Argument(..., help="dotted.path=value T2 diff, not applied."),
    eval_compare: str = typer.Option(
        ...,
        "--eval-compare",
        help="Eval comparison note required for any T2 change.",
    ),
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root"),
    tenant: str = typer.Option("default", "--tenant"),
    actor: str = typer.Option("cli", "--actor"),
) -> None:
    """File a T2 policy proposal. Does not write policy/default.json."""

    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.library import LibraryService
    from recertia.errors import RecertiaError
    from recertia.ledger import HashChainLedger
    from recertia.proposals.store import ProposalStore

    if not eval_compare.strip():
        typer.echo("T2 change requires --eval-compare", err=True)
        raise typer.Exit(code=2)
    policy_diff = parse_policy_assignment(assignment)
    store = ProposalStore(runs_root / "proposals.sqlite")
    ledger = HashChainLedger(runs_root / "runs" / tenant / "ledger.jsonl")
    try:
        rec = LibraryService(pdp=PolicyDecisionPoint.load(surface="cli")).propose_policy(
            _principal(ctx),
            store=store,
            tenant_id=tenant,
            policy_diff=policy_diff,
            rationale=eval_compare.strip(),
            actor=actor,
            ledger=ledger,
            created_by_job="policy-cli",
            extra_payload={"eval_comparison": eval_compare.strip()},
        )
    except RecertiaError as exc:
        from recertia.cli.exit import raise_exit

        raise_exit(exc)
    finally:
        store.close()
    typer.echo(json.dumps(rec.to_dict(), indent=2))
