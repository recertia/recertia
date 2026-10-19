"""Reliability errors. They subclass :class:`RecertiaError` directly."""

from __future__ import annotations

from recertia.errors import Busy, ConfigError, ExecutionUnavailable, RecertiaError

__all__ = [
    "Busy",
    "ConfigError",
    "ExecutionUnavailable",
    "IdempotencyMismatch",
    "LedgerLockTimeout",
    "LedgerUnavailable",
    "PreconditionRequired",
    "ProgramConflict",
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


class IdempotencyMismatch(RecertiaError):
    """Same idempotency key, different request hash."""

    code = "idempotency_mismatch"
    http_status = 409
    retryable = False
    exit_code = 5


class ProgramConflict(RecertiaError):
    """Program ``If-Match`` did not match the stored version."""

    code = "program_conflict"
    http_status = 409
    retryable = False
    exit_code = 5


class PreconditionRequired(RecertiaError):
    """``If-Match`` is required on an existing program."""

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

