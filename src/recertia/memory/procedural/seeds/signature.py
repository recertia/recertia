"""HMAC for hand-authored seed ``version.json`` when a ledger key is set.

Unsigned seeds still load while ``RECERTIA_LEDGER_HMAC_KEY`` is empty.
A configured key refuses an unsigned or tampered seed on read.
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

SIG_NAME = "version.hmac"


class SeedSignatureError(ValueError):
    """A seed file is unsigned or its HMAC does not match."""


def _key() -> bytes | None:
    from recertia.settings import load_settings

    secret = load_settings().ledger_hmac_key.strip()
    if not secret:
        return None
    return secret.encode("utf-8")


def _seed_ids() -> frozenset[str]:
    from recertia.memory.procedural.seeds.factories import SEED_SKILLS

    return frozenset(item.skill_id for item in SEED_SKILLS)


def stamp_seed(path: Path, skill_id: str) -> None:
    """Write ``version.hmac`` beside a seed file when the ledger key is set."""

    key = _key()
    if key is None or skill_id not in _seed_ids():
        return
    digest = hmac.new(key, path.read_bytes(), hashlib.sha256).hexdigest()
    path.with_name(SIG_NAME).write_text(digest + "\n", encoding="utf-8")


def verify_seed(path: Path, skill_id: str) -> None:
    """Check a seed HMAC. Non-seeds and an unset key with no signature pass."""

    if skill_id not in _seed_ids():
        return
    sig = path.with_name(SIG_NAME)
    key = _key()
    if key is None:
        if sig.is_file():
            raise SeedSignatureError("seed signature present but the ledger key is unset")
        return
    if not sig.is_file():
        raise SeedSignatureError(f"seed {skill_id} is unsigned")
    expected = hmac.new(key, path.read_bytes(), hashlib.sha256).hexdigest()
    got = sig.read_text(encoding="utf-8").strip()
    if len(got) != len(expected) or not hmac.compare_digest(got, expected):
        raise SeedSignatureError(f"seed {skill_id} signature does not verify")
