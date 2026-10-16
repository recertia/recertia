"""Incremental verify reads the suffix after a trusted sidecar."""

from __future__ import annotations

from pathlib import Path

from recertia.ledger import HashChainLedger
from recertia.ledger.sidecar import sidecar_path


def test_verify_scans_suffix_only(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = HashChainLedger(path)
    for index in range(4):
        ledger.append(actor="ci", action="write", target=f"a-{index}")
    saved = sidecar_path(path).read_bytes()
    for index in range(2):
        ledger.append(actor="ci", action="write", target=f"b-{index}")
    sidecar_path(path).write_bytes(saved)
    fresh = HashChainLedger(path)
    report = fresh.verify(full=False)
    assert report.full is False
    assert report.suffix_entries == 2
    assert report.entries == 6
    assert fresh.lines_parsed == 2
