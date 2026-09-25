"""Pre-promotion applicability gate (Ye et al. 2026; plan 2026-08 high-confidence items).

Kept off ``SkillVersion``: the version document is frozen and extra=forbid. The report is a
sidecar written to the ledger on rejection, never a mutable field on the skill.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EnvironmentModel(BaseModel):
    """Execution environment the distiller and applicability gate are allowed to assume."""

    model_config = ConfigDict(extra="forbid")

    tools: list[str] = Field(default_factory=list)
    backend: str = "container"
    limits: dict[str, object] = Field(default_factory=dict)


class ApplicabilityReason(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check: Literal["environment", "criterion", "contagion"]
    message: str


class ApplicabilityReport(BaseModel):
    """Outcome of the environment / criterion / contagion gate."""

    model_config = ConfigDict(extra="forbid")

    skill_id: str
    version: int
    ok: bool
    environment_ok: bool = True
    criterion_ok: bool = True
    contagion_ok: bool = True
    reasons: list[ApplicabilityReason] = Field(default_factory=list)
    structural_hash: str | None = None
