"""Optional MEA (Manage-Execute-Audit) subtask loop helpers.

Controller-owned scaffolding plus graph-engine binding. No graph nodes.
Activation is three-layer: global policy flag + per-Goal opt-in + runtime
strategy. Default remains single-request.
"""

from recertia.mea.activation import MeaActivation, resolve_mea_activation, should_note_fallback
from recertia.mea.controller import (
    MeaEarlyStop,
    enforce_round_budget,
    propose_subtask,
    require_fresh_auditor_context,
)
from recertia.mea.runtime import (
    apply_validate_audit,
    audit_after_validate,
    bind_after_intake,
    create_audited_state,
    resolve_from_run,
)
from recertia.mea.store import AuditedStateStore

__all__ = [
    "AuditedStateStore",
    "MeaActivation",
    "MeaEarlyStop",
    "apply_validate_audit",
    "audit_after_validate",
    "bind_after_intake",
    "create_audited_state",
    "enforce_round_budget",
    "propose_subtask",
    "require_fresh_auditor_context",
    "resolve_from_run",
    "resolve_mea_activation",
    "should_note_fallback",
]
