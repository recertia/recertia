"""Model client retries, deadlines, and the process-local circuit breaker."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from recertia.observability.metrics import metrics
from recertia.solver.model import ModelResponse, StubModelClient
from recertia.solver.providers import OpenAIModelClient, ProviderHTTPError
from recertia.solver.resilience import (
    CircuitBreaker,
    ModelUnavailable,
    backoff_s,
)
from tests.unit.lanes._fakes import Script, http_error, ok

GOOD = {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 10}}


def test_400_not_retried() -> None:
    script = Script(http_error(400, b'{"error":{"message":"nope"}}'))
    client = OpenAIModelClient(api_key="k", model_id="m", max_retries=2)
    with patch("urllib.request.urlopen", side_effect=script):
        with pytest.raises(ProviderHTTPError):
            client.complete("hi")
    assert len(script.requests) == 1
    assert client.spend.retries == 0
    assert client.spend.calls == 0


def test_429_honours_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr("recertia.solver.model.time.sleep", slept.append)
    script = Script(http_error(429, b"slow", {"Retry-After": "2"}), ok(GOOD))
    client = OpenAIModelClient(api_key="k", model_id="m-429")
    with patch("urllib.request.urlopen", side_effect=script):
        assert client.complete("hi").text == "ok"
    assert slept and slept[0] >= 2.0


def test_jitter_is_bounded() -> None:
    cap = 8.0
    samples = [
        backoff_s(attempt, base_s=0.25, cap_s=cap, retry_after_s=None) for attempt in range(100)
    ]
    assert samples
    assert all(0 < sample <= cap for sample in samples)


def test_late_response_is_charged(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([0.0, 5.0])
    monkeypatch.setattr("recertia.solver.model.time.monotonic", lambda: next(ticks))

    class Billed(StubModelClient):
        def _complete_once(self, prompt: str, *, system: str | None) -> ModelResponse:
            del prompt, system
            return ModelResponse(text="late", cost_usd=0.25, prompt_tokens=3, completion_tokens=4)

    before = metrics.value("recertia_model_cost_usd_late")
    client = Billed(timeout_s=1.0, max_retries=1, provider="stub", model_id="late")
    with pytest.raises(ModelUnavailable) as ei:
        client.complete("hi")
    assert ei.value.details["reason"] == "deadline"
    assert client.spend.cost_usd == pytest.approx(0.25)
    assert client.spend.calls == 1
    assert metrics.value("recertia_model_cost_usd_late") == pytest.approx(before + 0.25)


def test_circuit_opens_and_short_circuits() -> None:
    clock = {"now": 0.0}
    breaker = CircuitBreaker(threshold=5, window_s=30, open_s=30, clock=lambda: clock["now"])
    calls = {"n": 0}

    class Boom(StubModelClient):
        def _complete_once(self, prompt: str, *, system: str | None) -> ModelResponse:
            del prompt, system
            calls["n"] += 1
            raise ProviderHTTPError("HTTP 503", status=503, retry_after_s=None, body_excerpt="down")

    client = Boom(max_retries=0, provider="openai", model_id="circuit", breaker=breaker)
    for _ in range(5):
        with pytest.raises(ProviderHTTPError):
            client.complete("hi")
    assert calls["n"] == 5
    with pytest.raises(ModelUnavailable):
        client.complete("hi")
    assert calls["n"] == 5
    clock["now"] = 30.0
    with pytest.raises(ProviderHTTPError):
        client.complete("probe")
    assert calls["n"] == 6
