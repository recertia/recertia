"""The provenance ledger entry shape (specs §21).

Structural definition only. The hash-chain mechanics (canonical serialisation, ``entry_hash``
computation, append, verify) live in ``src/recertia/ledger/hashchain.py`` — that is runtime
behaviour, not a structural contract. This model exists in ``contracts/`` per ADR-0009 so the
entry shape is generated into ``schema/ledger_entry.schema.json`` rather than drifting from the
Python snippet in the docs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LedgerAction = Literal[
    "write",
    "advance_to_candidate",
    "quarantine_version",
    "deprecate",
    "policy_change",
    "lint_reject",
    "compress_skill",
    "revoke_lineage",
    "compose_block",
    "publish_patch_template",
    "lift_report",
    "faithfulness_report",
    "applicability_reject",
    "mea_activation_fallback",
]


class LedgerEntry(BaseModel):
    """One hash-chained record of a memory-plane write (specs §21).

    ``entry_hash`` is computed over the canonical serialisation of every other field; a chain
    is valid when each entry's ``prev_hash`` equals its predecessor's ``entry_hash`` and every
    entry's own ``entry_hash`` recomputes correctly.
    """

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=0)
    prev_hash: str
    entry_hash: str
    actor: str
    action: LedgerAction
    target: str
    evidence: dict = Field(default_factory=dict)
    at: datetime
