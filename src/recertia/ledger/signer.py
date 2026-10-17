"""HMAC-SHA256 ledger signatures.

The signature sits in ``evidence["_ledger_sig"]`` so the hash chain still covers it.
Unsigned lines verify until the first signed entry. Later lines must verify.
The key is ``Settings.ledger_hmac_key`` (``RECERTIA_LEDGER_HMAC_KEY``), not a file
next to the ledger.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from contracts.ledger import LedgerEntry

SIG_FIELD = "_ledger_sig"
SIG_ALG = "hmac-sha256"
KEY_ID = "hmac-sha256:1"


def signature_bytes(entry: LedgerEntry) -> bytes:
    """Canonical body with the signature field removed. ``entry_hash`` is excluded."""

    from recertia.ledger.hashchain import _canonical_bytes

    evidence = {key: value for key, value in entry.evidence.items() if key != SIG_FIELD}
    return _canonical_bytes(entry.model_copy(update={"evidence": evidence}))


def sign_entry(entry: LedgerEntry, key: bytes) -> dict[str, str]:
    digest = hmac.new(key, signature_bytes(entry), hashlib.sha256).hexdigest()
    return {"alg": SIG_ALG, "key_id": KEY_ID, "sig": digest}


def signature_ok(entry: LedgerEntry, key: bytes) -> bool:
    raw: Any = entry.evidence.get(SIG_FIELD)
    if not isinstance(raw, dict):
        return False
    sig = raw.get("sig")
    if raw.get("alg") != SIG_ALG or not isinstance(sig, str):
        return False
    expected = sign_entry(entry, key)["sig"]
    if len(sig) != len(expected):
        return False
    return hmac.compare_digest(expected, sig)


class HmacLedgerHook:
    """``LedgerHook`` that stamps ``evidence["_ledger_sig"]`` before the hash."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    def before_append(self, entry: LedgerEntry) -> LedgerEntry:
        evidence = {key: value for key, value in entry.evidence.items() if key != SIG_FIELD}
        stamped = sign_entry(entry.model_copy(update={"evidence": evidence}), self._key)
        evidence[SIG_FIELD] = stamped
        return entry.model_copy(update={"evidence": evidence})

    def after_append(self, entry: LedgerEntry) -> None:
        del entry


def hmac_hook_from_settings() -> HmacLedgerHook | None:
    from recertia.settings import load_settings

    secret = load_settings().ledger_hmac_key.strip()
    if not secret:
        return None
    return HmacLedgerHook(secret.encode("utf-8"))


def ledger_hmac_key() -> bytes | None:
    from recertia.settings import load_settings

    secret = load_settings().ledger_hmac_key.strip()
    if not secret:
        return None
    return secret.encode("utf-8")
