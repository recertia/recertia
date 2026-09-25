"""StoreReceipt contract (ADR-0020 P0). Empty compression_moves is valid."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from contracts.ledger import LedgerAction, LedgerEntry
from contracts.receipt import CompressionMove, ResidueManifestEntry, StoreReceipt, ValidityFrontier


def _receipt(**overrides: object) -> StoreReceipt:
    payload: dict[str, object] = {
        "receipt_id": "rcpt-1",
        "seq": 0,
        "prev_receipt_hash": "0" * 64,
        "receipt_hash": "a" * 64,
        "closed_by": "operator",
        "at": datetime(2026, 9, 23, tzinfo=timezone.utc),
    }
    payload.update(overrides)
    return StoreReceipt.model_validate(payload)


def test_empty_compression_moves_is_valid() -> None:
    receipt = _receipt()
    assert receipt.compression_moves == []
    assert receipt.residue_manifest == []
    assert receipt.fold_digest is None
    assert receipt.reviewed_git is None


def test_close_receipt_is_a_ledger_action() -> None:
    assert "close_receipt" in LedgerAction.__args__
    entry = LedgerEntry(
        seq=0,
        prev_hash="0" * 64,
        entry_hash="b" * 64,
        actor="operator",
        action="close_receipt",
        target="rcpt-1",
        at=datetime(2026, 9, 23, tzinfo=timezone.utc),
    )
    assert entry.action == "close_receipt"


def test_residue_and_move_round_trip() -> None:
    receipt = _receipt(
        residue_manifest=[
            ResidueManifestEntry(path="ops/log.json", content_hash="c" * 64, bytes=12)
        ],
        compression_moves=[
            CompressionMove(
                object_id="skill:demo",
                from_level="L0",
                to_level="L1",
                delta=1,
            )
        ],
        validity_frontier=ValidityFrontier(current_count=1, superseded_count=0),
    )
    again = StoreReceipt.model_validate(receipt.model_dump())
    assert again.residue_manifest[0].bytes == 12
    assert again.compression_moves[0].delta == 1


def test_negative_bytes_rejected() -> None:
    with pytest.raises(ValidationError):
        ResidueManifestEntry(path="x", content_hash="h", bytes=-1)
