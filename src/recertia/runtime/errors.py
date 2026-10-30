"""Reliability errors. They subclass :class:`RecertiaError` directly."""

from __future__ import annotations

from recertia.errors import Busy, ConfigError, ExecutionUnavailable, QuotaExceeded, RecertiaError

__all__ = [
    "Busy",
    "ConfigError",
    "ExecutionUnavailable",
    "IdempotencyInProgress",
    "IdempotencyMismatch",
    "InvalidCursor",
    "LedgerLockTimeout",
    "LedgerUnavailable",
    "PreconditionFailed",
    "PreconditionRequired",
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


class IdempotencyMismatch(RecertiaError):
    """An ``idempotency_key`` already used with a different request body. Nothing was written."""

    code = "idempotency_mismatch"
    http_status = 409
    retryable = False
    exit_code = 5


class IdempotencyInProgress(RecertiaError):
    """Another request with the same ``idempotency_key`` and body has not finished yet."""

    code = "idempotency_in_progress"
    http_status = 409
    retryable = True
    exit_code = 6


class PreconditionFailed(RecertiaError):
    """A program write named a version that is not the stored one (``If-Match`` failed)."""

    code = "precondition_failed"
    http_status = 412
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
