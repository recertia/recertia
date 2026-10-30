"""Runtime helpers: errors, ownership registry, workspace locks, tenancy."""

from recertia.runtime.errors import (
    LeaseLost,
    PreconditionRequired,
    ProgramConflict,
    RunIdExists,
    WorkspaceBusy,
)
from recertia.runtime.hop import OutboxRow, commit_hop
from recertia.runtime.lease import LeaseToken
from recertia.runtime.registry import ReserveResult, RunRegistry
from recertia.runtime.workspace_lock import WorkspaceLock, WorkspaceLockHandle

__all__ = [
    "LeaseLost",
    "LeaseToken",
    "OutboxRow",
    "PreconditionRequired",
    "ProgramConflict",
    "ReserveResult",
    "RunIdExists",
    "RunRegistry",
    "WorkspaceBusy",
    "WorkspaceLock",
    "WorkspaceLockHandle",
    "commit_hop",
]
