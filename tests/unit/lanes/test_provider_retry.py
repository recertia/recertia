"""Error / retry / backoff behaviour of ModelClient.complete over the real HTTP path."""

from __future__ import annotations

import socket
import threading
import time
import urllib.error
from unittest.mock import patch

import pytest

from recertia.solver.providers import OpenAIModelClient, ProviderError
from tests.unit.lanes._fakes import FakeResp, Script, http_error, ok

GOOD = {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr("recertia.solver.model._sleep", calls.append)
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
    ],
    ids=["503", "502-html", "timeout", "conn-reset"],
)
def test_transient_failure_then_success(failure: object, sleeps: list[float]) -> None:
    script = Script(failure, ok(GOOD))
    client = _client()
    with patch("urllib.request.urlopen", side_effect=script):
        assert client.complete("hi").text == "ok"
    assert len(script.requests) == 2
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= client.retry_backoff_s
    assert client.spend.retries == 1 and client.spend.calls == 1


def test_timeout_retry_sleep_count_ignores_stdlib_time_sleep(sleeps: list[float]) -> None:
    """URLError(socket.timeout) retries once; queue-poll time.sleep must not count.

    main@ab11e62 failed ``test_transient_failure_then_success[timeout]`` with
    ``assert 2 == 1`` on sleeps because the fixture patched stdlib ``time.sleep``.
    """

    stop = threading.Event()
    real_sleep = time.sleep

    def poll() -> None:
        while not stop.is_set():
            time.sleep(0.05)
            real_sleep(0.001)

    threading.Thread(target=poll, name="queue-poll", daemon=True).start()
    try:
        script = Script(urllib.error.URLError(socket.timeout("timed out")), ok(GOOD))
        client = _client()
        with patch("urllib.request.urlopen", side_effect=script):
            assert client.complete("hi").text == "ok"
        assert len(script.requests) == 2
        assert len(sleeps) == 1
        assert 0 < sleeps[0] <= client.retry_backoff_s
        assert client.spend.retries == 1 and client.spend.calls == 1
    finally:
        stop.set()


def test_exhausted_retries_raise_last_error_with_exponential_backoff(sleeps: list[float]) -> None:
    script = Script(*(http_error(500, b"oops") for _ in range(3)))
    client = _client(max_retries=2)
    with patch("urllib.request.urlopen", side_effect=script):
        with pytest.raises(ProviderError, match=r"HTTP 500 .*oops"):
            client.complete("hi")
    assert len(script.requests) == 3
    b = client.retry_backoff_s
    assert len(sleeps) == 2
    assert 0 < sleeps[0] <= b
    assert 0 < sleeps[1] <= 2 * b
    assert client.spend.calls == 0 and client.spend.cost_usd == 0.0


def test_malformed_json_is_not_retried(sleeps: list[float]) -> None:
    script = Script(FakeResp(b"{not json"), ok(GOOD))
    with patch("urllib.request.urlopen", side_effect=script):
        with pytest.raises(ProviderError):
            _client().complete("hi")
    assert len(script.requests) == 1 and sleeps == []


def test_error_body_is_truncated_in_message(sleeps: list[float]) -> None:
    del sleeps
    with patch("urllib.request.urlopen", side_effect=Script(http_error(500, b"A" * 10_000))):
        with pytest.raises(ProviderError) as ei:
            _client(max_retries=0).complete("hi")
    assert len(str(ei.value)) < 2300


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_non_retryable_4xx_fails_fast(code: int, sleeps: list[float]) -> None:
    script = Script(*(http_error(code, b'{"error":{"message":"nope"}}') for _ in range(3)))
    with patch("urllib.request.urlopen", side_effect=script):
        with pytest.raises(ProviderError):
            _client().complete("hi")
    assert len(script.requests) == 1 and sleeps == []


def test_slow_success_is_charged_and_not_returned(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """A body that arrives after the deadline is billed, then refused."""

    ticks = iter([0.0, 5.0])
    monkeypatch.setattr("recertia.solver.model.time.monotonic", lambda: next(ticks))
    client = OpenAIModelClient(api_key="k", model_id="m", timeout_s=1.0, max_retries=1)
    script = Script(ok(GOOD), ok(GOOD))
    from recertia.solver.resilience import ModelUnavailable

    with patch("urllib.request.urlopen", side_effect=script):
        with pytest.raises(ModelUnavailable) as ei:
            client.complete("hi")
    assert ei.value.details["reason"] == "deadline"
    assert len(script.requests) == 1 and script.requests[0][1] == 1.0
    assert client.spend.calls == 1 and client.spend.cost_usd > 0
    assert sleeps == []
