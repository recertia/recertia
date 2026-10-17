"""LedgerHook evidence is inside the hashed payload."""

from __future__ import annotations

from pathlib import Path

from contracts.ledger import LedgerEntry
from recertia.ledger import HashChainLedger
from recertia.ledger.hashchain import compute_entry_hash


class _Hook:
    def before_append(self, entry: LedgerEntry) -> LedgerEntry:
        evidence = dict(entry.evidence)
        evidence["signed"] = True
        return entry.model_copy(update={"evidence": evidence})

    def after_append(self, entry: LedgerEntry) -> None:
        self.seen = entry


def test_before_append_can_add_evidence(tmp_path: Path) -> None:
    hook = _Hook()
    ledger = HashChainLedger(tmp_path / "ledger.jsonl", hook=hook)
    entry = ledger.append(actor="ci", action="write", target="skill")
    assert entry.evidence["signed"] is True
    assert entry.entry_hash == compute_entry_hash(entry)
    ledger.verify(full=True)
