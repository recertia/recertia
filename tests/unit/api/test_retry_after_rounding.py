"""``recertia_error_response`` rounds ``retry_after_s`` up to whole seconds for ``Retry-After``."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from recertia.api.errors import recertia_error_response
from recertia.errors import RateLimited


@pytest.mark.parametrize(
    ("retry_after_s", "expected"),
    [
        (0.2, "1"),
        (1.2, "2"),
        (2.5, "3"),
        (0.999, "1"),
    ],
)
def test_fractional_retry_after_rounds_up(retry_after_s: float, expected: str) -> None:
    response = recertia_error_response(RateLimited("slow down", retry_after_s=retry_after_s))
    assert response.status_code == 429
    assert response.headers["Retry-After"] == expected


@pytest.mark.parametrize(
    ("retry_after_s", "expected"),
    [
        (1, "1"),
        (1.0, "1"),
        (5, "5"),
        (30.0, "30"),
        (0, "0"),
    ],
)
def test_whole_retry_after_is_unchanged(retry_after_s: float, expected: str) -> None:
    response = recertia_error_response(RateLimited("slow down", retry_after_s=retry_after_s))
    assert response.headers["Retry-After"] == expected


def test_missing_retry_after_sends_no_header() -> None:
    response = recertia_error_response(RateLimited("slow down"))
    assert response.status_code == 429
    assert "Retry-After" not in response.headers
