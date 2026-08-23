"""Three-layer MEA activation (Architect review required revision).

All three must be true for an MEA loop to start:
1. Global policy ImprovementFlags.mea_enabled
2. Per-Goal opt-in at intake
3. Runtime execution_strategy == "mea"

Missing any one falls back to the single-request path with a ledger note.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ExecutionStrategy = Literal["single", "mea"]


@dataclass(frozen=True)
class MeaActivation:
    policy_enabled: bool
    goal_opt_in: bool
    runtime_strategy: ExecutionStrategy
    active: bool
    fallback_reason: str | None


def resolve_mea_activation(
    *,
    policy_mea_enabled: bool = False,
    goal_mea_opt_in: bool = False,
    runtime_strategy: ExecutionStrategy = "single",
) -> MeaActivation:
    """Resolve whether the MEA loop may start. Default is inactive."""

    if not policy_mea_enabled:
        return MeaActivation(
            policy_enabled=False,
            goal_opt_in=goal_mea_opt_in,
            runtime_strategy=runtime_strategy,
            active=False,
            fallback_reason="policy_mea_disabled",
        )
    if not goal_mea_opt_in:
        return MeaActivation(
            policy_enabled=True,
            goal_opt_in=False,
            runtime_strategy=runtime_strategy,
            active=False,
            fallback_reason="goal_not_opted_in",
        )
    if runtime_strategy != "mea":
        return MeaActivation(
            policy_enabled=True,
            goal_opt_in=True,
            runtime_strategy=runtime_strategy,
            active=False,
            fallback_reason="runtime_strategy_not_mea",
        )
    return MeaActivation(
        policy_enabled=True,
        goal_opt_in=True,
        runtime_strategy="mea",
        active=True,
        fallback_reason=None,
    )
