"""Criteria types, split per the ADR-0003 amendment (refactor-plan B2).

Two measurement questions, two types, two timelines — and they never merge:

- ``TaskCriterion`` answers "did this run solve what the caller asked for?" Locked at
  ``intake``, before a skill is chosen. Sources: caller, task-class template, or a critic pass.
- ``SkillCertificationCriterion`` answers "does this skill version reliably do what it claims?"
  Authored at ``distill`` time; validated prospectively on independent fixtures before a version
  may leave ``candidate``. It never enters a run's required set (see ``contracts/run.py``:
  ``RunState.criteria`` is typed ``list[TaskCriterion]``, not a union).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contracts.common import Isolation, Lens

CriterionKind = Literal["command", "assertion", "schema", "metric", "judge"]

_REQUIRED_FIELDS_BY_KIND: dict[str, tuple[str, ...]] = {
    "command": ("run",),
    "assertion": ("expr",),
    "schema": ("target", "schema_ref"),
    "metric": ("metric", "op", "threshold"),
    "judge": ("rubric",),
}

# Executable fields shared by both criterion types — bind proofs across Task/Skill forms.
_EVIDENCE_HASH_FIELDS: tuple[str, ...] = (
    "id",
    "kind",
    "run",
    "expect_exit",
    "expr",
    "target",
    "schema_ref",
    "metric",
    "op",
    "threshold",
    "rubric",
    "isolation",
    "lens",
    "timeout_s",
    "weight",
)


def sensitivity_evidence_hash(
    criterion: "_CriterionFields", negative_fingerprint: str
) -> str:
    """Hash the executable criterion definition bound to a negative-fixture fingerprint."""

    data = criterion.model_dump(mode="json", exclude_none=False)
    payload = {key: data.get(key) for key in _EVIDENCE_HASH_FIELDS}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded + b"\0" + negative_fingerprint.encode()).hexdigest()


def sensitivity_proof_binds(
    criterion: "_CriterionFields", proof: "SensitivityProof | None" = None
) -> bool:
    """True when ``proof`` rejects and its evidence_hash matches a recomputation.

    Caller-minted ``rejected=True`` without a matching hash is not a valid proof.
    """

    bound = proof if proof is not None else criterion.sensitivity_proof
    if bound is None or not bound.rejected or not bound.evidence_hash:
        return False
    checked = bound.checked_against or ""
    if not checked.startswith("sha256:"):
        return False
    fingerprint = checked[len("sha256:") :]
    return bound.evidence_hash == sensitivity_evidence_hash(criterion, fingerprint)


def mint_rejecting_proof(
    criterion: "_CriterionFields",
    *,
    negative_fixture: str = "empty",
    fingerprint: str = "test-neg",
    checked_at: datetime | None = None,
) -> SensitivityProof:
    """Build a hash-bound rejecting proof for tests/seeds (does not re-run the criterion)."""

    return SensitivityProof(
        criterion_id=criterion.id,
        negative_fixture=negative_fixture,
        rejected=True,
        checked_at=checked_at or datetime(2026, 1, 1, tzinfo=timezone.utc),
        checked_against=f"sha256:{fingerprint}",
        evidence_hash=sensitivity_evidence_hash(criterion, fingerprint),
    )


class SensitivityProof(BaseModel):
    """Evidence that a criterion rejects a known-bad artifact (specs §15.2)."""

    model_config = ConfigDict(extra="forbid")

    criterion_id: str
    negative_fixture: str
    rejected: bool
    checked_at: datetime
    checked_against: str | None = None
    evidence_hash: str | None = Field(
        default=None,
        description=(
            "sha256 hash binding the criterion definition to the negative-fixture fingerprint "
            "used for this proof"
        ),
    )


class _CriterionFields(BaseModel):
    """Fields shared by both criterion types; not instantiated directly."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: CriterionKind
    run: str | None = None
    expect_exit: int = 0
    expr: str | None = None
    target: str | None = None
    schema_ref: str | None = None
    metric: str | None = None
    op: Literal["lt", "lte", "gt", "gte", "eq"] | None = None
    threshold: float | None = None
    rubric: str | None = None
    isolation: Isolation = "fresh_context"
    lens: Lens | None = None
    timeout_s: int = Field(default=300, ge=1, le=3600)
    weight: float = Field(default=1.0, ge=0.0, le=1.0)
    sensitivity_proof: SensitivityProof | None = None

    @model_validator(mode="after")
    def _kind_requires_fields(self) -> "_CriterionFields":
        missing = [
            field
            for field in _REQUIRED_FIELDS_BY_KIND[self.kind]
            if getattr(self, field) is None
        ]
        if missing:
            raise ValueError(
                f"criterion {self.id!r} of kind {self.kind!r} is missing required field(s): "
                f"{', '.join(missing)}"
            )
        return self

    @model_validator(mode="after")
    def _judge_requires_fresh_context(self) -> "_CriterionFields":
        if self.kind == "judge" and self.isolation != "fresh_context":
            raise ValueError(
                f"criterion {self.id!r} is kind='judge' but isolation={self.isolation!r}; "
                "judge criteria MUST be fresh_context (specs §26.3)"
            )
        return self

    @property
    def is_required(self) -> bool:
        return self.weight >= 1.0

    @property
    def is_preregistered_and_proven(self) -> bool:
        """A required criterion counts toward promotion only with a verified proof (specs §15).

        ``rejected`` alone is forgeable; the evidence hash must recompute against
        ``checked_against`` and the executable criterion definition.
        """

        return sensitivity_proof_binds(self)


class TaskCriterion(_CriterionFields):
    """Locked at ``intake`` for one run. Never authored by a chosen skill."""

    source: Literal["caller", "task_class_template", "critic"]
    preregistered: bool = True


def clamp_criterion_timeouts(
    criteria: list[TaskCriterion], *, ceiling_s: int
) -> list[TaskCriterion]:
    """Cap each criterion's ``timeout_s`` at ``ceiling_s`` (API ceiling after compile)."""

    clamped: list[TaskCriterion] = []
    for criterion in criteria:
        if criterion.timeout_s > ceiling_s:
            clamped.append(criterion.model_copy(update={"timeout_s": ceiling_s}))
        else:
            clamped.append(criterion)
    return clamped


class SkillCertificationCriterion(_CriterionFields):
    """Authored at ``distill`` time; validated on independent certification runs.

    ``preregistered`` here means registered before the *certification runs* (shadow trials,
    scheduled recertifications) that validate it — not before the transcript that produced the
    draft, which is impossible by construction. See the ADR-0003 amendment.
    """

    authored_by: Literal["distiller", "human"] = "distiller"
    preregistered: bool = Field(
        default=False,
        description="True once locked ahead of the certification runs that validate it.",
    )


class CriterionResult(BaseModel):
    """One scored criterion, for a run's result vector or a certification observation."""

    model_config = ConfigDict(extra="forbid")

    criterion_id: str
    kind: CriterionKind | None = None
    passed: bool
    weight: float = 1.0
    isolation: Isolation | None = None
    lens: Lens | None = None
    context_hash: str | None = Field(
        default=None,
        description="Hash of the exact context a judge saw; makes an isolation violation provable.",
    )
    exit_code: int | None = None
    output_excerpt: str = Field(default="", max_length=32768)
    errored: bool = False
    duration_s: float = 0.0
