"""Stable ``/v1`` error envelope (remaining-work RW-SUR)."""

from __future__ import annotations

import json
import math
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

_RETRYABLE_CODES = frozenset({"rate_limited", "lock_timeout", "worker_busy", "root_busy"})

_STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    412: "precondition_failed",
    413: "payload_too_large",
    428: "precondition_required",
    429: "rate_limited",
    500: "internal_error",
    503: "unavailable",
}


_MAX_RETRY_AFTER_S = 2**31 - 1


def retry_after_header(retry_after_s: object) -> str | None:
    """``Retry-After`` value for ``retry_after_s``: whole seconds, rounded up, or ``None``.

    ``None`` means "send no header". It is returned for a missing value and for any value
    that is not a usable delay: a bool, a non-number, ``nan``, ``inf``, a negative number, or
    more than ``2**31 - 1`` seconds. It never raises, because this runs while an error
    response is built and a failure here would turn that error into a 500.
    """

    if isinstance(retry_after_s, bool) or not isinstance(retry_after_s, (int, float)):
        return None
    # The range test also rejects nan and the infinities, and cannot overflow on a huge int.
    if not 0 <= retry_after_s <= _MAX_RETRY_AFTER_S:
        return None
    return str(math.ceil(retry_after_s))


def _retry_after_headers(retry_after_s: object) -> dict[str, str] | None:
    value = retry_after_header(retry_after_s)
    return None if value is None else {"Retry-After": value}


def error_body(
    *,
    code: str,
    message: str,
    run_id: str | None = None,
    retryable: bool | None = None,
) -> dict[str, Any]:
    if retryable is None:
        retryable = code in _RETRYABLE_CODES
    err: dict[str, Any] = {"code": code, "message": message, "retryable": bool(retryable)}
    if run_id:
        err["run_id"] = run_id
    return {"error": err}


def quota_error(exc: Exception, *, run_id: str | None = None) -> JSONResponse:
    """Map quota / in-flight exhaustion to the existing 429 envelope."""

    message = str(exc)
    if "max_in_flight" in message or "max_queue_depth" in message:
        return v1_error(
            429, code="worker_busy", message=message, run_id=run_id, retryable=True
        )
    return v1_error(
        429, code="budget_exhausted", message=message, run_id=run_id, retryable=False
    )


def recertia_error_response(exc: Any) -> JSONResponse:
    """``RecertiaError`` → existing ``{"error": ...}`` envelope plus Retry-After."""

    from recertia.errors import RecertiaError

    if not isinstance(exc, RecertiaError):
        raise TypeError("expected RecertiaError")
    body = exc.error_body()
    headers = _retry_after_headers(exc.retry_after_s)
    return JSONResponse(status_code=exc.http_status, content=body, headers=headers)


def v1_error(
    status_code: int,
    *,
    code: str,
    message: str,
    run_id: str | None = None,
    retryable: bool | None = None,
    retry_after_s: float | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=error_body(code=code, message=message, run_id=run_id, retryable=retryable),
        headers=_retry_after_headers(retry_after_s),
    )


class V1HTTPError(Exception):
    """Raise from new ``/v1`` routes so the envelope is used instead of FastAPI ``detail``."""

    def __init__(
        self,
        status_code: int,
        *,
        code: str,
        message: str,
        run_id: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.run_id = run_id
        self.retryable = retryable

    def response(self) -> JSONResponse:
        return v1_error(
            self.status_code,
            code=self.code,
            message=self.message,
            run_id=self.run_id,
            retryable=self.retryable,
        )


def _detail_message(detail: Any) -> str:
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict) and isinstance(detail.get("message"), str):
        return str(detail["message"])
    try:
        return json.dumps(detail)
    except TypeError:
        return str(detail)


def _is_v1_path(path: str) -> bool:
    return path == "/v1" or path.startswith("/v1/")


def envelope_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Wrap ``HTTPException`` as the ``/v1`` envelope; leave ``{detail}`` elsewhere.

    FastAPI ``RequestValidationError`` (422) is not an ``HTTPException`` and keeps
    ``{detail: ...}``. ``/health`` is not under ``/v1``.
    """

    headers = getattr(exc, "headers", None)
    if not _is_v1_path(request.url.path):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=headers,
        )
    detail = exc.detail
    if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
        return JSONResponse(status_code=exc.status_code, content=detail, headers=headers)
    message = _detail_message(detail)
    code = _STATUS_CODES.get(exc.status_code, "http_error")
    lowered = message.lower()
    if exc.status_code == 429 and ("in-flight" in lowered or "worker" in lowered):
        code = "worker_busy"
    return v1_error(
        exc.status_code,
        code=code,
        message=message,
        retryable=code in _RETRYABLE_CODES,
    )
