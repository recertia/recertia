"""Torn-tail repair. Truncation is only for a partial EOF, never a mid-chain break."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from recertia.fsutil.atomic import atomic_write_bytes
from recertia.runtime.errors import LedgerUnavailable

if TYPE_CHECKING:
    from recertia.ledger.hashchain import HashChainLedger


@dataclass(frozen=True)
class RepairReport:
    status: str
    dropped_bytes: int
    prefix_entries: int = 0
    prefix_sha256: str = ""


def repair_locked(ledger: HashChainLedger, *, actor: str, dry_run: bool) -> RepairReport:
    """Run under the ledger flock. ``ledger`` is the open chain."""

    from recertia.ledger.hashchain import GENESIS_HASH, read_tail

    last, complete_len = read_tail(ledger.path)
    del last
    size = ledger.path.stat().st_size if ledger.path.exists() else 0
    raw = ledger.path.read_bytes() if size else b""
    if size > complete_len:
        prefix = raw[:complete_len]
        try:
            entries = ledger._entries_from_bytes(prefix)
            ledger._verify_chain(entries, start_seq=0, prev_hash=GENESIS_HASH)
        except Exception as exc:
            raise LedgerUnavailable(
                "ledger hash mismatch is not a torn tail; restore from backup"
            ) from exc
        dropped = size - complete_len
        prefix_sha = hashlib.sha256(prefix).hexdigest()
        if dry_run:
            return RepairReport(
                status="dry_run",
                dropped_bytes=dropped,
                prefix_entries=len(entries),
                prefix_sha256=prefix_sha,
            )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        dest = ledger.path.parent / "quarantine" / stamp / "torn-tail.bin"
        atomic_write_bytes(dest, raw[complete_len:], durability=ledger._durability)
        fd = os.open(ledger.path, os.O_RDWR)
        try:
            os.ftruncate(fd, complete_len)
            os.fsync(fd)
        finally:
            os.close(fd)
        ledger._tip = (
            len(entries),
            entries[-1].entry_hash if entries else GENESIS_HASH,
        )
        ledger._append_locked(
            actor=actor,
            action="ledger_repaired",
            target=f"ledger:{ledger.path}",
            evidence={
                "dropped_bytes": dropped,
                "prefix_sha256": prefix_sha,
                "prefix_entries": len(entries),
                "reason": "torn_tail",
            },
        )
        return RepairReport(
            status="repaired",
            dropped_bytes=dropped,
            prefix_entries=len(entries),
            prefix_sha256=prefix_sha,
        )
    try:
        ledger._verify_unlocked(full=True)
    except Exception as exc:
        raise LedgerUnavailable(
            "ledger hash mismatch is not a torn tail; restore from backup"
        ) from exc
    return RepairReport(status="already_clean", dropped_bytes=0)
