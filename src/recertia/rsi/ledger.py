"""Hash-chained append-only integrity ledger. Spec A4, R11."""

from __future__ import annotations

import hashlib
import json
from typing import Optional


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Ledger:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    def head(self) -> str:
        if not self.entries:
            return "genesis"
        return self.entries[-1]["receipt"]

    def append(self, kind: str, body: dict) -> str:
        prev = self.head()
        payload = canonical({"prev": prev, "kind": kind, "body": body})
        receipt = sha256_text(payload)
        self.entries.append(
            {"seq": len(self.entries), "prev": prev, "kind": kind, "body": body, "receipt": receipt}
        )
        return receipt

    def get(self, receipt: str) -> Optional[dict]:
        for entry in self.entries:
            if entry["receipt"] == receipt:
                return entry
        return None

    def up_to(self, receipt: str) -> list[dict]:
        if receipt == "genesis":
            return []
        out = []
        for entry in self.entries:
            out.append(entry)
            if entry["receipt"] == receipt:
                return out
        raise KeyError(f"unknown ledger head: {receipt}")

    def verify_chain(self) -> None:
        prev = "genesis"
        for entry in self.entries:
            if entry["prev"] != prev:
                raise AssertionError("ledger prev link broken")
            payload = canonical({"prev": entry["prev"], "kind": entry["kind"], "body": entry["body"]})
            if sha256_text(payload) != entry["receipt"]:
                raise AssertionError("ledger receipt mismatch")
            prev = entry["receipt"]

    def contains_text(self, needle: str) -> bool:
        return needle in canonical(self.entries)
