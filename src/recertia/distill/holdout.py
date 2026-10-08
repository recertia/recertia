"""Holdout gate. Distill must call this before a candidate write."""

from __future__ import annotations

from pathlib import Path

from recertia.evals.contribution import load_protocol, reject_holdout


def distill_holdout_block(fixture_id: str | None, protocol_path: Path | None) -> str | None:
    if protocol_path is None or not protocol_path.exists():
        return None
    return reject_holdout(load_protocol(protocol_path), fixture_id)
