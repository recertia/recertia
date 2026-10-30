"""Runtime helpers: errors, ownership registry, workspace locks, tenancy."""

from recertia.runtime.errors import (
    IdempotencyInProgress,
    IdempotencyMismatch,
    LeaseLost,
    PreconditionFailed,
    PreconditionRequired,
    RunIdExists,
    WorkspaceBusy,
)
from recertia.runtime.hop import OutboxRow, commit_hop
from recertia.runtime.lease import LeaseToken
from recertia.runtime.registry import ReserveResult, RunRegistry
from recertia.runtime.workspace_lock import WorkspaceLock, WorkspaceLockHandle

__all__ = [
    "IdempotencyInProgress",
    "IdempotencyMismatch",
    "LeaseLost",
    "LeaseToken",
    "OutboxRow",
    "PreconditionFailed",
    "PreconditionRequired",
    "ReserveResult",
    "RunIdExists",
    "RunRegistry",
    "WorkspaceBusy",
    "WorkspaceLock",
    "WorkspaceLockHandle",
    "commit_hop",
]
