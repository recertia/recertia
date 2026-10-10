"""Failure signalling and classification, per ADR-0008 (refactor-plan B4)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

FailureClass = Literal[
    "environment", "tool", "retrieval", "plan", "execution", "criteria", "budget", "merge"
]

FAILURE_CLASSES: tuple[FailureClass, ...] = (
    "environment",
    "tool",
    "retrieval",
    "plan",
    "execution",
    "criteria",
    "budget",
    "merge",
)

CLASSES_EXCLUDED_FROM_TRUST: frozenset[str] = frozenset({"environment", "tool", "budget", "merge"})


class FailureSignal(BaseModel):
    """Raised by the orchestrator, solver, validator, or join — never inferred after the fact.

    ``classify_failure``'s only precondition is that a ``FailureSignal`` exists on the run
    state (ADR-0008); this replaces the old, unsatisfiable-for-most-classes precondition of
    "some required criterion failed."

    ``class_hint`` is an optional structured producer signal (e.g. budget preflight via
    ``budget_excess``). When set, ``classify_failure`` prefers it over detail substring matching.
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["orchestrator", "solver", "validator", "join"]
    detail: str
    at: datetime
    class_hint: FailureClass | None = None


class FailureVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_class: FailureClass
    evidence: list[str] = []
    implicated_skill: dict[str, Any] | None = None
    counts_against_trust: bool
    escalate_to_human: bool = False

    @property
    def is_consistent(self) -> bool:
        expected_counts = self.failure_class not in CLASSES_EXCLUDED_FROM_TRUST
        expected_escalate = self.failure_class == "criteria"
        return self.counts_against_trust == expected_counts and self.escalate_to_human == expected_escalate
