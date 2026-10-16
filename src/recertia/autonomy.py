"""Lifecycle thresholds shared by memory and review. Not a review import."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AutonomyConfig:
    """Shadow promotion / quarantine / retirement thresholds (specs §8, §24)."""

    shadow_min_successes: int = 5
    shadow_min_applications: int = 8
    shadow_min_lift: float = 0.05
    quarantine_consecutive_failures: int = 2
    evidence_floor: int = 30
    retirement_threshold: float = 0.05
    active_cap_per_task_class: int = 50
    shadow_slots_per_task_class: int = 3
    incumbent_grace_applications: int = 5
    curation_prior_self_distilled: float = 0.85


DEFAULT_AUTONOMY = AutonomyConfig()
HARSH_AUTONOMY = AutonomyConfig(
    evidence_floor=20, retirement_threshold=0.0, active_cap_per_task_class=3
)
