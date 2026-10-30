"""Reliability errors. They subclass :class:`RecertiaError` directly."""

from __future__ import annotations

from recertia.errors import Busy, ConfigError, ExecutionUnavailable, QuotaExceeded, RecertiaError

__all__ = [
    "Busy",
    "ConfigError",
    "ExecutionUnavailable",
    "InvalidCursor",
    "LedgerLockTimeout",
    "LedgerUnavailable",
    "PreconditionRequired",
    "ProgramConflict",
    "QuotaExceeded",
    "RunIdExists",
    "StateCorrupt",
    "WorkspaceBusy",
    "LeaseLost",
]


class StateCorrupt(RecertiaError):
    code = "state_corrupt"
    http_status = 503
    retryable = False
    exit_code = 7


class LedgerUnavailable(RecertiaError):
    code = "ledger_unavailable"
    http_status = 503
    retryable = False
    exit_code = 7


class LedgerLockTimeout(RecertiaError):
    code = "lock_timeout"
    http_status = 503
    retryable = True
    exit_code = 6


class RunIdExists(RecertiaError):
    """Primary-key conflict on ``runs`` — ``run_id`` is already reserved."""

    code = "run_id_exists"
    http_status = 409
    retryable = False
    exit_code = 5


class ProgramConflict(RecertiaError):
    """A program write named a version that is not the stored one."""

    code = "program_conflict"
    http_status = 409
    retryable = False
    exit_code = 5


class PreconditionRequired(RecertiaError):
    """A replace of an existing program arrived without ``If-Match``."""

    code = "precondition_required"
    http_status = 428
    retryable = False
    exit_code = 2


class WorkspaceBusy(Busy):
    """Another owner holds the workspace flock."""

    code = "workspace_busy"
    http_status = 409
    retryable = True
    exit_code = 6


class LeaseLost(Busy):
    """A write lost the fencing compare; the lease was stolen."""

    code = "lease_lost"
    http_status = 409
    retryable = True
    exit_code = 6


class InvalidCursor(RecertiaError):
    """A list cursor that is malformed, oversized, non-canonical or has bad fields."""

    code = "invalid_cursor"
    http_status = 400
    retryable = False
    exit_code = 2
