"""HMAC ledger signatures. Unsigned lines stay valid until the first signed entry."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.ledger import LedgerEntry
from recertia.ledger import HashChainLedger, LedgerVerificationError
from recertia.ledger.hashchain import GENESIS_HASH, compute_entry_hash
from recertia.ledger.signer import SIG_FIELD


def _rehash(path: Path) -> None:
    prev = GENESIS_HASH
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = LedgerEntry.model_validate_json(line)
        draft = entry.model_copy(update={"prev_hash": prev, "entry_hash": "pending"})
        digest = compute_entry_hash(draft)
        lines.append(draft.model_copy(update={"entry_hash": digest}).model_dump_json())
        prev = digest
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_check_anchor_is_a_skip(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from recertia.cli.main import app

    result = CliRunner().invoke(
        app, ["ledger", "verify", "--runs-root", str(tmp_path), "--check-anchor"]
    )
    assert result.exit_code == 0, result.output
    assert "anchor check skipped" in result.output


def test_ledger_actor_is_the_principal_subject() -> None:
    from recertia.ports.identity import Principal, ledger_actor

    principal = Principal.local_operator(actor="ada")
    assert ledger_actor(principal) == "ada"
    assert ledger_actor(principal) == principal.subject


def test_unsigned_legacy_verifies_without_a_key(tmp_path: Path) -> None:
    ledger = HashChainLedger(tmp_path / "ledger.jsonl")
    ledger.append(actor="a", action="write", target="skill", at=datetime.now(timezone.utc))
    ledger.verify(check_sig=True)


def test_signed_append_and_tampered_evidence_fails_check_sig(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_LEDGER_HMAC_KEY", "test-ledger-key")
    path = tmp_path / "ledger.jsonl"
    ledger = HashChainLedger(path)
    entry = ledger.append(actor="a", action="write", target="skill", at=datetime.now(timezone.utc))
    assert isinstance(entry.evidence[SIG_FIELD], dict)
    ledger.append(actor="a", action="write", target="skill-2", at=datetime.now(timezone.utc))
    ledger.verify(check_sig=True)

    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["evidence"]["note"] = "rewritten"
    lines[0] = json.dumps(tampered)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _rehash(path)

    resigned = HashChainLedger(path)
    resigned.verify()
    with pytest.raises(LedgerVerificationError, match="signature"):
        resigned.verify(check_sig=True)


def test_unsigned_entries_allowed_until_cutover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = HashChainLedger(path)
    ledger.append(actor="a", action="write", target="legacy", at=datetime.now(timezone.utc))
    monkeypatch.setenv("RECERTIA_LEDGER_HMAC_KEY", "test-ledger-key")
    ledger.append(actor="a", action="write", target="cutover", at=datetime.now(timezone.utc))
    ledger.verify(check_sig=True)

    monkeypatch.delenv("RECERTIA_LEDGER_HMAC_KEY")
    ledger.append(actor="a", action="write", target="after", at=datetime.now(timezone.utc))
    monkeypatch.setenv("RECERTIA_LEDGER_HMAC_KEY", "test-ledger-key")
    with pytest.raises(LedgerVerificationError, match="signature"):
        ledger.verify(check_sig=True)


def test_signed_chain_without_the_key_fails_check_sig(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_LEDGER_HMAC_KEY", "test-ledger-key")
    path = tmp_path / "ledger.jsonl"
    HashChainLedger(path).append(actor="a", action="write", target="skill", at=datetime.now(timezone.utc))
    monkeypatch.delenv("RECERTIA_LEDGER_HMAC_KEY")
    with pytest.raises(LedgerVerificationError, match="not configured"):
        HashChainLedger(path).verify(check_sig=True)
