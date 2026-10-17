"""Reliability errors. They subclass :class:`RecertiaError` directly."""

from __future__ import annotations

from recertia.errors import ConfigError, ExecutionUnavailable, RecertiaError

__all__ = [
    "ConfigError",
    "ExecutionUnavailable",
    "LedgerLockTimeout",
    "LedgerUnavailable",
    "StateCorrupt",
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
