"""One error base for HTTP, CLI, and library callers.

HTTP bodies stay ``{"error": {code, message, retryable, ...}}``. ``retry_after_s``
is the caller's signal to set ``Retry-After``.
"""

from __future__ import annotations

from typing import ClassVar, Mapping


class RecertiaError(Exception):
    code: ClassVar[str] = "internal_error"
    http_status: ClassVar[int] = 500
    retryable: ClassVar[bool] = False
    exit_code: ClassVar[int] = 7

    def __init__(
        self,
        message: str,
        *,
        run_id: str | None = None,
        details: Mapping[str, object] | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.run_id = run_id
        self.details: dict[str, object] = dict(details or {})
        self.retry_after_s = retry_after_s

    def error_body(self) -> dict[str, object]:
        err: dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.run_id is not None:
            err["run_id"] = self.run_id
        if self.details:
            err["details"] = self.details
        return {"error": err}


class ConfigError(RecertiaError):
    code = "invalid_config"
    http_status = 400
    retryable = False
    exit_code = 2


class ExecutionUnavailable(RecertiaError):
    code = "execution_unavailable"
    http_status = 503
    retryable = False
    exit_code = 7


class NotFound(RecertiaError):
    code = "not_found"
    http_status = 404
    retryable = False
    exit_code = 1


class Unauthorized(RecertiaError):
    code = "unauthorized"
    http_status = 401
    retryable = False
    exit_code = 3


class Forbidden(RecertiaError):
    code = "forbidden"
    http_status = 403
    retryable = False
    exit_code = 3


class Conflict(RecertiaError):
    code = "conflict"
    http_status = 409
    retryable = False
    exit_code = 5


class QuotaExceeded(RecertiaError):
    code = "quota_exceeded"
    http_status = 429
    retryable = False
    exit_code = 4


class Busy(RecertiaError):
    code = "busy"
    http_status = 409
    retryable = True
    exit_code = 6


class InvalidTenantId(RecertiaError, ValueError):
    code = "invalid_input"
    http_status = 400
    retryable = False
    exit_code = 2


class RateLimited(RecertiaError):
    code = "rate_limited"
    http_status = 429
    retryable = True
    exit_code = 4
