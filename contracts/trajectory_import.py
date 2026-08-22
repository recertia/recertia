"""External trajectory import (ADR-0019). Append-only candidate material.

Imported trajectories enter episodic memory. They are never promoted to approved
skills until Recertia re-validates them under locked criteria and a control-arm
lift interval. A long-lived computer is an optional affordance, never the default
security boundary.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contracts.criteria import TaskCriterion

ImportSource = Literal["grok_bot_recording", "grok_bot_run", "external_demo", "synthetic"]
ImportOutcome = Literal["solved", "failed", "partial"]
ComputerBackend = Literal["grok_bot", "other"]
IsolationMode = Literal["ephemeral", "long_lived_opt_in"]


class EnvironmentDescriptor(BaseModel):
    """Where the external run happened. Required for import."""

    model_config = ConfigDict(extra="forbid")

    os: str = Field(min_length=1)
    browser: str | None = None
    tools: list[str] = Field(default_factory=list)
    versions: dict[str, str] = Field(default_factory=dict)
    network_policy: str = Field(min_length=1)


class ProvenanceBundle(BaseModel):
    """Who captured the trajectory, when, and any attestation."""

    model_config = ConfigDict(extra="forbid")

    actor: str = Field(min_length=1)
    captured_at: datetime
    attestation: str | None = None
    signature: str | None = None


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1)
    ref: str = Field(min_length=1)


class TrajectoryStep(BaseModel):
    """One tool/action plus observation. Not a Recertia graph event."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=0)
    action: str = Field(min_length=1)
    tool: str | None = None
    observation: str | None = None
    ok: bool | None = None


class ExternalComputerExecutor(BaseModel):
    """Optional affordance. Default execution remains --rm containers."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["external_computer"] = "external_computer"
    backend: ComputerBackend = "other"
    allowlist_policy: str = Field(min_length=1)
    session_ttl: timedelta = Field(default=timedelta(hours=1))
    isolation_mode: IsolationMode = "ephemeral"


class TrajectoryImport(BaseModel):
    """Append-only import record. Never mutates an existing Recertia run."""

    model_config = ConfigDict(extra="forbid")

    import_id: str = Field(min_length=1)
    source: ImportSource
    source_ref: str = Field(min_length=1)
    captured_at: datetime
    environment: EnvironmentDescriptor
    steps: list[TrajectoryStep] = Field(default_factory=list)
    outcome: ImportOutcome
    criteria_snapshot: list[TaskCriterion] = Field(default_factory=list)
    provenance: ProvenanceBundle
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    reexecutable: bool = False
    task_class: str | None = None

    @model_validator(mode="after")
    def _provenance_complete(self) -> "TrajectoryImport":
        if not self.provenance.actor.strip():
            raise ValueError("provenance.actor is required")
        if not self.environment.os.strip() or not self.environment.network_policy.strip():
            raise ValueError("environment.os and environment.network_policy are required")
        return self
