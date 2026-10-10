"""Mocked API contract tests for the provider lane (recertia.solver.providers)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from recertia.solver.providers import AnthropicModelClient, OpenAIModelClient, ProviderError
from tests.unit.lanes._fakes import Script, ok

OPENAI_OK = {
    "id": "chatcmpl-1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}}],
    "usage": {"prompt_tokens": 7, "completion_tokens": 3},
}
ANTHROPIC_OK = {
    "content": [{"type": "text", "text": "hello"}],
    "usage": {"input_tokens": 7, "output_tokens": 3},
}


def _openai(**kw: object) -> OpenAIModelClient:
    return OpenAIModelClient(api_key="sk-test", model_id="gpt-test", max_retries=0, **kw)  # type: ignore[arg-type]


def _anthropic() -> AnthropicModelClient:
    return AnthropicModelClient(api_key="ak-test", model_id="claude-test", max_retries=0)


def test_openai_request_shape_and_usage_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("RECERTIA_OPENAI_EXTRA_BODY", "RECERTIA_OPENAI_EXTRA_HEADERS", "RECERTIA_OPENAI_MAX_TOKENS"):
        monkeypatch.delenv(var, raising=False)
    script = Script(ok(OPENAI_OK))
    with patch("urllib.request.urlopen", side_effect=script):
        resp = _openai().complete("hi", system="sys")
    req, timeout = script.requests[0]
    body = json.loads(req.data)
    assert req.get_method() == "POST" and timeout == 60.0
    assert req.full_url == "https://api.openai.com/v1/chat/completions"
    assert req.get_header("Authorization") == "Bearer sk-test"
    assert body["model"] == "gpt-test"
    assert body["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    assert (resp.text, resp.prompt_tokens, resp.completion_tokens) == ("hello", 7, 3)


def test_anthropic_request_shape_and_usage_mapping() -> None:
    script = Script(ok(ANTHROPIC_OK))
    with patch("urllib.request.urlopen", side_effect=script):
        resp = _anthropic().complete("hi", system="sys")
    req, _ = script.requests[0]
    body = json.loads(req.data)
    assert req.get_header("X-api-key") == "ak-test"
    assert req.get_header("Anthropic-version") == "2023-06-01"
    assert body["system"] == "sys" and body["messages"] == [{"role": "user", "content": "hi"}]
    assert isinstance(body["max_tokens"], int)
    assert (resp.text, resp.prompt_tokens, resp.completion_tokens) == ("hello", 7, 3)


@pytest.mark.parametrize(
    "payload, match",
    [
        ({"choices": []}, "missing choices"),
        ({"choices": [{"message": None}]}, "missing message"),
        ({"choices": [{"message": {"content": ""}}]}, "no text"),
        ({"error": {"message": "boom", "code": 502}}, r"boom \(code=502\)"),  # 200-with-error body
        ([1, 2, 3], "unexpected JSON payload"),
    ],
)
def test_openai_schema_drift_raises_provider_error(payload: object, match: str) -> None:
    with patch("urllib.request.urlopen", side_effect=Script(ok(payload))):
        with pytest.raises(ProviderError, match=match):
            _openai().complete("hi")


@pytest.mark.parametrize(
    "payload, match",
    [
        ({"content": []}, "missing content blocks"),
        ({"content": [{"type": "tool_use", "id": "x"}]}, "no text"),
    ],
)
def test_anthropic_schema_drift_raises_provider_error(payload: object, match: str) -> None:
    with patch("urllib.request.urlopen", side_effect=Script(ok(payload))):
        with pytest.raises(ProviderError, match=match):
            _anthropic().complete("hi")


def test_missing_usage_falls_back_to_estimate() -> None:
    payload = {"choices": [{"message": {"content": "x" * 40}}]}
    with patch("urllib.request.urlopen", side_effect=Script(ok(payload))):
        resp = _openai().complete("y" * 80)
    assert resp.prompt_tokens == 20 and resp.completion_tokens == 10
