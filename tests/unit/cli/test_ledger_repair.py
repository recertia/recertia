"""Torn-tail repair and verify exit codes."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from recertia.cli.main import app
from recertia.ledger import HashChainLedger
from recertia.runtime.errors import LedgerUnavailable

runner = CliRunner()


def _fill(path: Path, n: int = 10) -> None:
    ledger = HashChainLedger(path)
    for index in range(n):
        ledger.append(actor="ci", action="write", target=f"t-{index}")


def test_repair_truncates_torn_tail_and_records_ledger_repaired(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    _fill(path, 10)
    with path.open("ab") as handle:
        handle.write(b"not-a-complete-line")
    with pytest.raises(LedgerUnavailable):
        HashChainLedger(path).append(actor="ci", action="write", target="after")
    result = runner.invoke(
        app,
        ["ledger", "repair", "--truncate-torn-tail", "--runs-root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    again = HashChainLedger(path).append(actor="ci", action="write", target="after")
    assert again.seq == 11
    actions = [entry.action for entry in HashChainLedger(path).entries()]
    assert actions[-2] == "ledger_repaired"
    verified = runner.invoke(app, ["ledger", "verify", "--runs-root", str(tmp_path)])
    assert verified.exit_code == 0, verified.output


def test_repair_refuses_mid_file_corruption(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    _fill(path, 6)
    raw = bytearray(path.read_bytes())
    raw[len(raw) // 2] ^= 0x01
    path.write_bytes(raw)
    before = path.stat().st_size
    result = runner.invoke(
        app,
        ["ledger", "repair", "--truncate-torn-tail", "--runs-root", str(tmp_path)],
    )
    assert result.exit_code == 7, result.output
    assert path.stat().st_size == before


def test_verify_exit_code_is_2(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    _fill(path, 2)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace("t-0", "t-9")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    invalid = runner.invoke(app, ["ledger", "verify", "--runs-root", str(tmp_path)])
    assert invalid.exit_code == 2
    empty = tmp_path / "empty"
    empty.mkdir()
    valid = runner.invoke(app, ["ledger", "verify", "--runs-root", str(empty)])
    assert valid.exit_code == 0
