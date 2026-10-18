"""CLI: store-receipt index (ADR-0020 P1). Advisory verify; no backup --receipts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

receipt_app = typer.Typer(help="Store receipts (index + CLI; backup --receipts stays off).")


def register_receipt_commands(app: typer.Typer) -> None:
    app.add_typer(receipt_app, name="receipt")


@receipt_app.command("show")
def receipt_show(
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root"),
) -> None:
    """Print receipts.jsonl as JSON. Missing index is an empty list."""

    from recertia.errors import RecertiaError
    from recertia.ops.receipts import load_receipts
    from recertia.persistence.layout import RuntimeLayout

    layout = RuntimeLayout(runs_root).local_runs()
    try:
        receipts = load_receipts(layout.receipts_path)
    except RecertiaError as exc:
        from recertia.cli.exit import raise_exit

        raise_exit(exc)
    payload = {"receipts": [item.model_dump(mode="json") for item in receipts]}
    typer.echo(json.dumps(payload, indent=2))


@receipt_app.command("verify")
def receipt_verify(
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root"),
) -> None:
    """Advisory check of receipts.jsonl and close_receipt ledger evidence.

    A missing genesis receipt prints a warning and exits 0. Invalid evidence fails.
    """

    from recertia.errors import RecertiaError
    from recertia.ledger import HashChainLedger, LedgerVerificationError
    from recertia.ops.receipts import has_genesis, load_receipts, verify_close_receipt_evidence
    from recertia.persistence.layout import RuntimeLayout
    from recertia.runtime.errors import LedgerUnavailable

    layout = RuntimeLayout(runs_root).local_runs()
    try:
        receipts = load_receipts(layout.receipts_path)
        if layout.ledger_path.exists():
            ledger = HashChainLedger(layout.ledger_path)
            for entry in ledger.entries():
                if entry.action == "close_receipt":
                    verify_close_receipt_evidence(entry.evidence)
    except RecertiaError as exc:
        from recertia.cli.exit import raise_exit

        raise_exit(exc)
    except (LedgerVerificationError, LedgerUnavailable) as exc:
        typer.echo(f"receipts INVALID: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if not has_genesis(receipts):
        typer.echo("warning: no genesis receipt", err=True)
    typer.echo(f"receipts OK: {len(receipts)} receipts")


@receipt_app.command("close")
def receipt_close(
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root"),
    actor: str = typer.Option("operator", "--actor"),
    genesis: bool = typer.Option(
        False,
        "--genesis",
        help="Inventory the live tree as an upgrade_genesis receipt.",
    ),
    skills_root: Optional[Path] = typer.Option(
        None, "--skills-root", help="Checkout that owns skills/ (default: cwd)."
    ),
) -> None:
    """Close a receipt. ``--genesis`` inventories the tree; ``store`` never closes."""

    from recertia.errors import RecertiaError
    from recertia.ledger import HashChainLedger
    from recertia.ops.receipts import build_close_receipt, close_receipt
    from recertia.persistence.layout import RuntimeLayout

    layout = RuntimeLayout(runs_root).local_runs()
    layout.runs_root.mkdir(parents=True, exist_ok=True)
    try:
        receipt = build_close_receipt(
            layout.runs_root,
            genesis=genesis,
            receipts_path=layout.receipts_path,
            skills_root=skills_root,
        )
        ledger = HashChainLedger(layout.ledger_path)
        entry = close_receipt(ledger, receipt, receipts_path=layout.receipts_path, actor=actor)
    except RecertiaError as exc:
        from recertia.cli.exit import raise_exit

        raise_exit(exc)
    typer.echo(
        json.dumps(
            {
                "receipt_id": receipt.receipt_id,
                "ledger_seq": entry.seq,
                "closed_by": receipt.closed_by,
                "genesis": receipt.genesis,
            },
            indent=2,
        )
    )
