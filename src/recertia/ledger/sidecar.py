"""Verified ledger tip. A matching prefix hash lets verify scan only the suffix."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from recertia.fsutil.atomic import Durability, atomic_write_json


@dataclass(frozen=True)
class TipSidecar:
    entries: int
    next_seq: int
    tip_hash: str
    prefix_bytes: int
    prefix_sha256: str
    durability: str

    def to_json(self) -> dict[str, object]:
        return {
            "entries": self.entries,
            "next_seq": self.next_seq,
            "tip_hash": self.tip_hash,
            "prefix_bytes": self.prefix_bytes,
            "prefix_sha256": self.prefix_sha256,
            "durability": self.durability,
        }


def sidecar_path(ledger_path: Path) -> Path:
    """``ledger.jsonl`` → ``ledger.tip.json`` beside the chain."""

    return ledger_path.with_name(f"{ledger_path.stem}.tip.json")


def load_sidecar(path: Path) -> TipSidecar | None:
    """Return a parsed sidecar, or None when it is missing or unreadable."""

    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return TipSidecar(
            entries=int(raw["entries"]),
            next_seq=int(raw["next_seq"]),
            tip_hash=str(raw["tip_hash"]),
            prefix_bytes=int(raw["prefix_bytes"]),
            prefix_sha256=str(raw["prefix_sha256"]),
            durability=str(raw.get("durability", "normal")),
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def sidecar_matches(ledger_path: Path, tip: TipSidecar) -> bool:
    """True when the first ``prefix_bytes`` hash to ``prefix_sha256``."""

    try:
        size = ledger_path.stat().st_size if ledger_path.exists() else 0
    except OSError:
        return False
    if tip.prefix_bytes < 0 or size < tip.prefix_bytes:
        return False
    digest = hashlib.sha256()
    try:
        with ledger_path.open("rb") as handle:
            remaining = tip.prefix_bytes
            while remaining:
                chunk = handle.read(min(remaining, 1024 * 1024))
                if not chunk:
                    return False
                digest.update(chunk)
                remaining -= len(chunk)
    except OSError:
        return False
    return digest.hexdigest() == tip.prefix_sha256


def write_sidecar(
    ledger_path: Path,
    *,
    entries: int,
    tip_hash: str,
    durability_name: str,
    durability: Durability,
) -> None:
    """Rewrite the sidecar for the current complete file."""

    data = ledger_path.read_bytes() if ledger_path.exists() else b""
    tip = TipSidecar(
        entries=entries,
        next_seq=entries,
        tip_hash=tip_hash,
        prefix_bytes=len(data),
        prefix_sha256=hashlib.sha256(data).hexdigest(),
        durability=durability_name,
    )
    atomic_write_json(sidecar_path(ledger_path), tip.to_json(), durability=durability)
