"""Ledger tape integrity under crash/tamper (src/recertia/ledger/hashchain.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from recertia.ledger import HashChainLedger
from recertia.ledger.hashchain import LedgerVerificationError


def _fill(path: Path, n: int = 5) -> HashChainLedger:
    led = HashChainLedger(path)
    for i in range(n):
        led.append(actor="ci", action="write", target=f"run-{i}", evidence={"i": i})
    led.verify()
    return led


def test_restart_continues_chain(tmp_path: Path) -> None:
    p = tmp_path / "ledger.jsonl"
    _fill(p, 3)
    led2 = HashChainLedger(p)  # fresh process: no cached tip
    e = led2.append(actor="ci", action="write", target="after-restart")
    assert e.seq == 3
    led2.verify()


@pytest.mark.parametrize("mutation", ["edit_evidence", "drop_middle", "swap_lines", "dup_line"])
def test_verify_detects_tamper(tmp_path: Path, mutation: str) -> None:
    p = tmp_path / "ledger.jsonl"
    _fill(p)
    lines = p.read_text().splitlines()
    if mutation == "edit_evidence":
        row = json.loads(lines[2])
        row["evidence"] = {"i": 999}
        lines[2] = json.dumps(row)
    elif mutation == "drop_middle":
        del lines[2]
    elif mutation == "swap_lines":
        lines[1], lines[2] = lines[2], lines[1]
    elif mutation == "dup_line":
        lines.insert(3, lines[2])
    p.write_text("\n".join(lines) + "\n")
    with pytest.raises(LedgerVerificationError):
        HashChainLedger(p).verify()


def test_torn_tail_write_is_detected_not_silently_extended(tmp_path: Path) -> None:
    """Simulate a crash mid-append: last line half-written, no trailing newline.

    Contract we want: verify() fails loudly, and append() refuses rather than gluing a new
    record onto the torn fragment (which would corrupt two entries instead of one).
    """
    p = tmp_path / "ledger.jsonl"
    _fill(p, 3)
    raw = p.read_bytes()
    last = raw.rstrip(b"\n").rsplit(b"\n", 1)[1]
    p.write_bytes(raw[: len(raw) - 1 - len(last) // 2])  # chop half of the last record
    with pytest.raises(Exception):
        HashChainLedger(p).verify()
    with pytest.raises(Exception):
        HashChainLedger(p).append(actor="ci", action="write", target="post-crash")
