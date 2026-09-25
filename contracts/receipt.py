"""StoreReceipt contract (ADR-0020).

Structural definition of the published durability atom. Hash-chain append and
backup/restore behaviour live in ``src/recertia/`` — that is runtime, not this
contract. Empty ``compression_moves`` is valid (P1–P4 must not wait on lift).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CompressionLevel = Literal["L0", "L1", "L2", "L3"]
CompressionDelta = Literal[-1, 0, 1]
ReceiptCloser = Literal["curator", "sleep", "operator", "upgrade_genesis"]


class ResidueManifestEntry(BaseModel):
    """One cited working-set object. P1 uses path+hash; P3 may pack these."""

    model_config = ConfigDict(extra="forbid")

    path: str
    content_hash: str
    bytes: int = Field(ge=0)


class CompressionMove(BaseModel):
    """One L0–L3 promotion or demotion recorded on a receipt."""

    model_config = ConfigDict(extra="forbid")

    object_id: str
    from_level: CompressionLevel
    to_level: CompressionLevel
    delta: CompressionDelta
    evidence_ref: str | None = None


class ValidityFrontier(BaseModel):
    """Counts of current vs superseded fold rows. Schema columns land in P5."""

    model_config = ConfigDict(extra="forbid")

    current_count: int = Field(ge=0, default=0)
    superseded_count: int = Field(ge=0, default=0)
    note: str | None = None


class StoreReceipt(BaseModel):
    """Published durability atom (ADR-0020). Empty compression_moves is valid."""

    model_config = ConfigDict(extra="forbid")

    receipt_id: str
    seq: int = Field(ge=0)
    prev_receipt_hash: str
    receipt_hash: str
    closed_by: ReceiptCloser
    reviewed_git: str | None = None
    fold_digest: str | None = None
    residue_cid: str | None = None
    residue_manifest: list[ResidueManifestEntry] = Field(default_factory=list)
    compression_moves: list[CompressionMove] = Field(default_factory=list)
    validity_frontier: ValidityFrontier | None = None
    ledger_seq: int | None = Field(default=None, ge=0)
    at: datetime
