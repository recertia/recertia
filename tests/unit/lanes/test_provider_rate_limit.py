"""429 / Retry-After handling for the provider and arXiv lanes."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from recertia.jobs.arxiv import ArxivClient, ArxivFetchError
from recertia.solver.providers import OpenAIModelClient, ProviderError
from tests.unit.lanes._fakes import FakeResp, Script, http_error, ok

GOOD = {"choices": [{"message": {"content": "ok"}}]}
RL_BODY = b'{"error":{"message":"Rate limit exceeded","code":429}}'


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr("recertia.solver.model.time.sleep", calls.append)
    return calls


def test_429_is_retried_and_surfaces_gateway_code(sleeps: list[float]) -> None:
    script = Script(http_error(429, RL_BODY, {"Retry-After": "2"}), ok(GOOD))
    with patch("urllib.request.urlopen", side_effect=script):
        assert OpenAIModelClient(api_key="k", model_id="m").complete("hi").text == "ok"
    assert len(script.requests) == 2


def test_persistent_429_error_message_names_code(sleeps: list[float]) -> None:
    script = Script(*(http_error(429, RL_BODY) for _ in range(3)))
    with patch("urllib.request.urlopen", side_effect=script):
        with pytest.raises(ProviderError, match=r"HTTP 429 .*code=429"):
            OpenAIModelClient(api_key="k", model_id="m").complete("hi")


@pytest.mark.xfail(
    strict=True, reason="bug 10: Retry-After ignored; backoff is retry_backoff_s * 2**n (0.05s)"
)
def test_429_honours_retry_after(sleeps: list[float]) -> None:
    script = Script(http_error(429, RL_BODY, {"Retry-After": "2"}), ok(GOOD))
    with patch("urllib.request.urlopen", side_effect=script):
        OpenAIModelClient(api_key="k", model_id="m").complete("hi")
    assert sleeps and sleeps[0] >= 2.0


def test_arxiv_429_raises_typed_error() -> None:
    client = ArxivClient(min_interval_s=0)
    with patch("urllib.request.urlopen", side_effect=Script(http_error(429, b"", {"Retry-After": "5"}))):
        with pytest.raises(ArxivFetchError, match="HTTP 429"):
            client.search("transformers")


def test_arxiv_throttle_enforces_min_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    # monotonic(): throttle#1, stamp#1, throttle#2 (0.5s later), stamp#2
    clock = iter([99.0, 100.0, 100.5, 103.0])
    monkeypatch.setattr("recertia.jobs.arxiv.time.monotonic", lambda: next(clock))
    slept: list[float] = []
    monkeypatch.setattr("recertia.jobs.arxiv.time.sleep", slept.append)
    feed = b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
    client = ArxivClient(min_interval_s=3.0)
    with patch("urllib.request.urlopen", side_effect=Script(FakeResp(feed), FakeResp(feed))):
        client.search("a")
        client.search("b")
    assert slept == [pytest.approx(2.5)]
