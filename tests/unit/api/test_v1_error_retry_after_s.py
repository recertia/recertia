"""``v1_error`` takes an optional ``retry_after_s`` and one validated helper builds ``Retry-After`` (#145)."""

from __future__ import annotations

import inspect
import json
import math
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from recertia.api.errors import (
    recertia_error_response,
    retry_after_header,
    v1_error,
)
from recertia.errors import RateLimited

_MAX = 2**31 - 1

# Values that are not a usable delay: they must give no header and must not raise.
_INVALID: list[Any] = [
    float("nan"),
    float("inf"),
    float("-inf"),
    -1,
    -1.0,
    -0.5,
    -1e-9,
    -1e300,
    True,
    False,
    "2",
    "abc",
    "",
    b"2",
    [1],
    Decimal("1.5"),
    _MAX + 1,
    1e300,
    10**400,
    -(10**400),
]

# Valid values and the header they give.
_VALID: list[tuple[Any, str]] = [
    (0, "0"),
    (0.0, "0"),
    (-0.0, "0"),
    (1, "1"),
    (1.0, "1"),
    (30.0, "30"),
    (5e-324, "1"),
    (0.2, "1"),
    (0.999, "1"),
    (1.0000001, "2"),
    (2.5, "3"),
    (1e9 + 0.5, "1000000001"),
    (_MAX, str(_MAX)),
    (float(_MAX), str(_MAX)),
]


def _body(resp: Any) -> Any:
    return json.loads(bytes(resp.body))


def test_v1_error_retry_after_s_defaults_to_none() -> None:
    param = inspect.signature(v1_error).parameters["retry_after_s"]
    assert param.default is None
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


def test_v1_error_without_retry_after_s_sends_no_header() -> None:
    resp = v1_error(503, code="unavailable", message="try later")
    assert resp.status_code == 503
    assert "Retry-After" not in resp.headers


def test_v1_error_with_none_sends_no_header() -> None:
    resp = v1_error(503, code="unavailable", message="try later", retry_after_s=None)
    assert "Retry-After" not in resp.headers


@pytest.mark.parametrize(("value", "expected"), _VALID)
def test_v1_error_sets_the_rounded_up_header(value: Any, expected: str) -> None:
    resp = v1_error(429, code="rate_limited", message="slow down", retry_after_s=value)
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == expected


@pytest.mark.parametrize("value", _INVALID, ids=repr)
def test_v1_error_invalid_retry_after_s_sends_no_header(value: Any) -> None:
    resp = v1_error(429, code="rate_limited", message="slow down", retry_after_s=value)
    assert resp.status_code == 429
    assert "Retry-After" not in resp.headers


@pytest.mark.parametrize("value", [None, 0.2, 7, float("nan"), -3, True], ids=repr)
def test_v1_error_retry_after_s_does_not_change_the_body(value: Any) -> None:
    plain = v1_error(429, code="rate_limited", message="slow down", run_id="r1")
    with_value = v1_error(429, code="rate_limited", message="slow down", run_id="r1", retry_after_s=value)
    assert _body(with_value) == _body(plain)
    assert "retry_after_s" not in json.dumps(_body(with_value))


@pytest.mark.parametrize(("value", "expected"), _VALID)
def test_recertia_error_response_sets_the_rounded_up_header(value: Any, expected: str) -> None:
    resp = recertia_error_response(RateLimited("slow down", retry_after_s=value))
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == expected


@pytest.mark.parametrize("value", _INVALID, ids=repr)
def test_recertia_error_response_invalid_retry_after_s_sends_no_header(value: Any) -> None:
    resp = recertia_error_response(RateLimited("slow down", retry_after_s=value))
    assert resp.status_code == 429
    assert "Retry-After" not in resp.headers
    assert _body(resp) == RateLimited("slow down").error_body()


@pytest.mark.parametrize("value", [None, *[v for v, _ in _VALID], *_INVALID], ids=repr)
def test_both_builders_agree_on_the_header(value: Any) -> None:
    from_v1 = v1_error(429, code="rate_limited", message="x", retry_after_s=value)
    from_exc = recertia_error_response(RateLimited("x", retry_after_s=value))
    assert from_v1.headers.get("Retry-After") == from_exc.headers.get("Retry-After")
    assert from_v1.headers.get("Retry-After") == retry_after_header(value)


def test_retry_after_header_is_none_for_none() -> None:
    assert retry_after_header(None) is None


@pytest.mark.parametrize(("value", "expected"), _VALID)
def test_retry_after_header_matches_ceil_for_valid_values(value: Any, expected: str) -> None:
    assert retry_after_header(value) == expected == str(math.ceil(value))


@pytest.mark.parametrize("value", _INVALID, ids=repr)
def test_retry_after_header_never_raises_and_is_none_for_invalid_values(value: Any) -> None:
    assert retry_after_header(value) is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), -1, 0.2, 7])
def test_registered_handler_keeps_the_status_for_any_retry_after_s(tmp_path: Path, value: float) -> None:
    """Through the app's real ``RecertiaError`` handler a bad value is no 500 and no bad header."""

    from fastapi.testclient import TestClient

    from recertia.api import create_app

    app = create_app(root=tmp_path / "api-root", skills_root=tmp_path / "skills")

    def boom() -> None:
        raise RateLimited("slow down", retry_after_s=value)

    app.add_api_route("/__retry_after_probe", boom)
    res = TestClient(app, raise_server_exceptions=False).get("/__retry_after_probe")

    assert res.status_code == 429
    assert res.json()["error"]["code"] == "rate_limited"
    header = res.headers.get("Retry-After")
    assert header == retry_after_header(value)
    if header is not None:
        assert header.isdigit()
