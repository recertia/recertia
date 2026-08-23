"""Optional MEA (Manage-Execute-Audit) subtask loop.

Off by default. Three-layer activation required: global policy flag +
per-Goal opt-in + runtime strategy. No new graph nodes.
"""

from recertia.mea.activation import (
    MeaActivation,
    MeaPolicyFlags,
    activation_decision,
)
from recertia.mea.controller import (
    MeaControllerResult,
    MeaRoundOutcome,
    early_stop_reason,
    run_mea_round,
)
from recertia.mea.auditor_gate import (
    AuditorInstance,
    require_distinct_auditor,
    require_fresh_context,
)

__all__ = [
    "AuditorInstance",
    "MeaActivation",
    "MeaControllerResult",
    "MeaPolicyFlags",
    "MeaRoundOutcome",
    "activation_decision",
    "early_stop_reason",
    "require_distinct_auditor",
    "require_fresh_context",
    "run_mea_round",
]
