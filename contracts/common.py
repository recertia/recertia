"""Shared enums and primitive types referenced across contracts."""

from __future__ import annotations

from typing import Literal

Plane = Literal["procedural", "semantic", "episodic", "affordance", "policy"]

Curation = Literal[
    "human_authored",
    "mined_from_human_artifact",
    "mined_from_paper",
    "self_distilled",
]

Derivation = Literal["success_transcript", "failure_cluster", "mined_artifact", "hand_authored"]

Lens = Literal["correctness", "currency", "provenance", "scope", "safety"]

Isolation = Literal["fresh_context", "inherits_context"]

ResourceKind = Literal["file", "path", "service", "rate_limit", "lock", "external_system"]

ResourceMode = Literal["read", "write", "exclusive"]

Strategy = Literal["apply", "adapt", "scratch", "portfolio", "decomposition", "abstain"]

Arm = Literal["treatment", "control", "shadow", "practice"]

Terminal = Literal["solved", "unsolved", "abstained", "rejected", "error"]

# Lifecycle values for SkillStatus (ADR-0007). See docs/specifications.md §2.2.
Lifecycle = Literal[
    "draft",
    "candidate",
    "shadow",
    "approved",
    "benched",
    "needs_recert",
    "deprecated",
    "quarantined",
]

RETRIEVABLE_LIFECYCLES: frozenset[str] = frozenset({"approved", "shadow"})
