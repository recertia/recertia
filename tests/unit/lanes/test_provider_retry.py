"""Error / retry / backoff behaviour of ModelClient.complete over the real HTTP path."""

from __future__ import annotations

import socket
import urllib.error
from unittest.mock import patch

import pytest

from recertia.solver.providers import OpenAIModelClient, ProviderError
from tests.unit.lanes._fakes import FakeResp, Script, http_error, ok

GOOD = {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr("recertia.solver.model.time.sleep", calls.append)
    return calls


def _client(max_retries: int = 2) -> OpenAIModelClient:
    return OpenAIModelClient(api_key="k", model_id="m", max_retries=max_retries)


@pytest.mark.parametrize(
    "failure",
    [
        http_error(503, b"upstream unavailable"),
        http_error(502, b"<html>bad gateway</html>"),
        urllib.error.URLError(socket.timeout("timed out")),
        urllib.error.URLError(ConnectionResetError()),
        FakeResp(b"{not json"),
    ],
    ids=["503", "502-html", "timeout", "conn-reset", "malformed-json"],
)
def test_transient_failure_then_success(failure: object, sleeps: list[float]) -> None:
    script = Script(failure, ok(GOOD))
    client = _client()
    with patch("urllib.request.urlopen", side_effect=script):
        assert client.complete("hi").text == "ok"
    assert len(script.requests) == 2
    assert sleeps == [pytest.approx(client.retry_backoff_s)]
    assert client.spend.retries == 1 and client.spend.calls == 1


def test_exhausted_retries_raise_last_error_with_exponential_backoff(sleeps: list[float]) -> None:
    script = Script(*(http_error(500, b"oops") for _ in range(3)))
    client = _client(max_retries=2)
    with patch("urllib.request.urlopen", side_effect=script):
        with pytest.raises(ProviderError, match=r"HTTP 500 .*oops"):
            client.complete("hi")
    assert len(script.requests) == 3
    b = client.retry_backoff_s
    assert sleeps == [pytest.approx(b), pytest.approx(2 * b)]
    assert client.spend.calls == 0 and client.spend.cost_usd == 0.0


def test_error_body_is_truncated_in_message(sleeps: list[float]) -> None:
    del sleeps
    with patch("urllib.request.urlopen", side_effect=Script(http_error(500, b"A" * 10_000))):
        with pytest.raises(ProviderError) as ei:
            _client(max_retries=0).complete("hi")
    assert len(str(ei.value)) < 2300


@pytest.mark.xfail(
    strict=True, reason="bug 10: ModelClient.complete retries every Exception, incl. 4xx auth errors"
)
@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_non_retryable_4xx_fails_fast(code: int, sleeps: list[float]) -> None:
    script = Script(*(http_error(code, b'{"error":{"message":"nope"}}') for _ in range(3)))
    with patch("urllib.request.urlopen", side_effect=script):
        with pytest.raises(ProviderError):
            _client().complete("hi")
    assert len(script.requests) == 1 and sleeps == []


def test_slow_success_is_treated_as_timeout_and_retried(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    ticks = iter([0.0, 5.0, 10.0, 10.1])  # attempt1 start/end (5s), attempt2 start/end
    monkeypatch.setattr("recertia.solver.model.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("recertia.telemetry.emit_in_run", lambda *a, **k: None)
    client = OpenAIModelClient(api_key="k", model_id="m", timeout_s=1.0, max_retries=1)
    script = Script(ok(GOOD), ok(GOOD))
    with patch("urllib.request.urlopen", side_effect=script):
        assert client.complete("hi").text == "ok"
    assert len(script.requests) == 2 and script.requests[0][1] == 1.0
    assert client.spend.calls == 1 and client.spend.retries == 1
    assert len(sleeps) == 1
