"""``v1_error`` reports ``root_busy`` as retryable, like ``RootBusy.error_body()`` (#145)."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException, Request

from recertia.api.errors import envelope_http_exception, error_body, v1_error
from recertia.persistence.root_lock import RootBusy


def test_v1_error_root_busy_is_retryable() -> None:
    resp = v1_error(503, code="root_busy", message="the runtime root is busy")

    assert resp.status_code == 503
    assert json.loads(bytes(resp.body)) == {
        "error": {
            "code": "root_busy",
            "message": "the runtime root is busy",
            "retryable": True,
        }
    }


def test_v1_error_root_busy_matches_the_exception_body() -> None:
    exc = RootBusy("the runtime root is busy")
    resp = v1_error(
        exc.http_status,
        code=exc.code,
        message=exc.message,
    )

    assert json.loads(bytes(resp.body)) == exc.error_body()


def test_error_body_explicit_retryable_still_wins_for_root_busy() -> None:
    body = error_body(code="root_busy", message="busy", retryable=False)

    assert body["error"]["retryable"] is False


def test_envelope_http_exception_keeps_a_root_busy_envelope_retryable() -> None:
    """A ``root_busy`` envelope built by ``error_body`` and carried as an ``HTTPException`` detail
    comes out of ``envelope_http_exception`` unchanged. This goes through the envelope helper
    only, not through a route."""

    request = Request(
        {"type": "http", "method": "GET", "path": "/v1/runs/r", "headers": [], "query_string": b""}
    )
    exc = HTTPException(503, detail=error_body(code="root_busy", message="the runtime root is busy"))

    resp = envelope_http_exception(request, exc)

    assert resp.status_code == 503
    assert json.loads(bytes(resp.body)) == {
        "error": {
            "code": "root_busy",
            "message": "the runtime root is busy",
            "retryable": True,
        }
    }


@pytest.mark.parametrize(
    ("code", "retryable"),
    [
        ("rate_limited", True),
        ("lock_timeout", True),
        ("worker_busy", True),
        ("not_found", False),
        ("unavailable", False),
    ],
)
def test_v1_error_other_codes_keep_their_retryable_flag(code: str, retryable: bool) -> None:
    resp = v1_error(503, code=code, message="m")

    assert json.loads(bytes(resp.body))["error"]["retryable"] is retryable
