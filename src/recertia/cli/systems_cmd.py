"""CLI: emit AgentSysBench six-property snapshot from in-process telemetry or JSONL."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer


def register_systems_commands(app: typer.Typer) -> None:
    app.command("systems")(systems_cmd)


def systems_cmd(
    spans: Optional[Path] = typer.Option(None, "--spans", help="Telemetry JSONL to fold."),
    output: Optional[Path] = typer.Option(None, "--output"),
    brief: bool = typer.Option(False, "--brief", help="Stuck jobs, lift-by-class, redundancy."),
) -> None:
    """Print the six-property snapshot. Does not claim lift or a 4.6× memory cut."""

    from recertia.ops.systems import snapshot_from_events
    from recertia.telemetry import get_telemetry

    events: list[object]
    if spans is not None:
        events = []
        for line in spans.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if payload.get("type") == "event":
                events.append(payload)
    else:
        events = list(get_telemetry().events)
    snap = snapshot_from_events(events)
    if brief:
        from contracts.policy import COMPUTER_USE_TASK_CLASSES
        from recertia.ops.operator_brief import brief_from_events

        payload = brief_from_events(
            events, task_classes=list(COMPUTER_USE_TASK_CLASSES)
        ).as_dict()
        payload["six_properties"] = snap.as_dict()
        text = json.dumps(payload, indent=2)
    else:
        text = json.dumps(snap.as_dict(), indent=2)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
    typer.echo(text)
