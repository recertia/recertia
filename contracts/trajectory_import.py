"""TrajectoryImport contract (external trajectories + MEA attachment).

Complements the 2026-08-22 external-trajectories plan and the MEA
AuditedTaskState design. Import is append-only. Incomplete provenance or
missing environment is rejected. ``reexecutable=True`` trajectories may be
replayed under MEA but imported claims are never treated as auditor truth;
the environment must be re-verified under Recertia criteria.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contracts.audited_task_state import ArtifactRef, ProvenanceBundle
from contracts.criteria import TaskCriterion


class EnvironmentDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    os: str | None = None
    browser: str | None = None
    tools: list[str] = Field(default_factory=list)
    versions: dict[str, str] = Field(default_factory=dict)
    network_policy: Literal["none", "allowlist", "open"] = "none"
    notes: str | None = None


class TrajectoryStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=0)
    action: str
    target: str | None = None
    input: str | None = None
    observed: str | None = None
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    at: datetime | None = None


class TrajectoryImport(BaseModel):
    """External trajectory offered for episodic storage and optional MEA replay."""

    model_config = ConfigDict(extra="forbid")

    import_id: str
    source: Literal[
        "grok_bot_recording",
        "grok_bot_run",
        "external_demo",
        "synthetic",
        "operator",
    ]
    source_ref: str
    captured_at: datetime
    environment: EnvironmentDescriptor
    steps: list[TrajectoryStep] = Field(min_length=1)
    outcome: Literal["solved", "failed", "partial"]
    criteria_snapshot: list[TaskCriterion] = Field(default_factory=list)
    provenance: ProvenanceBundle
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    reexecutable: bool = False
    mea_goal_id: str | None = Field(
        default=None,
        description="When set, import may be replayed under an AuditedTaskState for this Goal",
    )
    require_auditor_reverify: bool = Field(
        default=True,
        description="Imported claims never become auditor truth without environment re-check",
    )

    @model_validator(mode="after")
    def _provenance_and_environment_required(self) -> TrajectoryImport:
        if not self.provenance.source:
            raise ValueError("provenance.source is required")
        if not self.source_ref.strip():
            raise ValueError("source_ref must be non-empty")
        env = self.environment
        if (
            not env.os
            and not env.browser
            and not env.tools
            and not env.versions
            and env.network_policy == "none"
            and not env.notes
        ):
            raise ValueError("environment descriptor is incomplete")
        return self


def import_may_promote(imp: TrajectoryImport) -> tuple[bool, str]:
    """Promotion requires reexecutable + auditor re-verification path."""
    if not imp.reexecutable:
        return False, "not_reexecutable"
    if not imp.require_auditor_reverify:
        return False, "auditor_reverify_disabled"
    if not imp.criteria_snapshot:
        return False, "missing_criteria_snapshot"
    return True, "ok"


def import_may_attach_mea(imp: TrajectoryImport) -> tuple[bool, str]:
    """MEA attachment requires reexecutable + mea_goal_id + re-verify."""
    if not imp.reexecutable:
        return False, "not_reexecutable"
    if not imp.mea_goal_id:
        return False, "missing_mea_goal_id"
    if not imp.require_auditor_reverify:
        return False, "auditor_reverify_disabled"
    return True, "ok"
