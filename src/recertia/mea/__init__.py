"""Optional MEA (Manage-Execute-Audit) subtask loop helpers.

Controller-owned scaffolding only. No graph nodes. Activation is three-layer:
global policy flag + per-Goal opt-in + runtime strategy. Default remains
single-request.
"""

from recertia.mea.activation import MeaActivation, resolve_mea_activation
from recertia.mea.controller import (
    MeaEarlyStop,
    enforce_round_budget,
    propose_subtask,
    require_fresh_auditor_context,
)

__all__ = [
    "MeaActivation",
    "MeaEarlyStop",
    "enforce_round_budget",
    "propose_subtask",
    "require_fresh_auditor_context",
    "resolve_mea_activation",
]
