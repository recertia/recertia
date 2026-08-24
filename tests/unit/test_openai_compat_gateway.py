"""OpenAI-compatible gateway extras (OpenRouter headers / body)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from recertia.solver.pricing import cost_is_vendor_exact, estimate_cost_usd
from recertia.solver.providers import (
    OpenAIModelClient,
    ProviderError,
    openai_compat_extra_body,
    openai_compat_headers,
)


def test_openai_compat_headers_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECERTIA_OPENAI_HTTP_REFERER", "https://example.com/app")
    monkeypatch.setenv("RECERTIA_OPENAI_TITLE", "Recertia")
    monkeypatch.setenv(
        "RECERTIA_OPENAI_EXTRA_HEADERS",
        '{"X-Custom":"abc"}',
    )
    headers = openai_compat_headers()
    assert headers["HTTP-Referer"] == "https://example.com/app"
    assert headers["X-OpenRouter-Title"] == "Recertia"
    assert headers["X-Title"] == "Recertia"
    assert headers["X-Custom"] == "abc"


def test_openai_compat_extra_body_blocks_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "RECERTIA_OPENAI_EXTRA_BODY",
        '{"temperature":0.1,"model":"evil","messages":[]}',
    )
    extra = openai_compat_extra_body()
    assert extra == {"temperature": 0.1}


def test_openai_client_sends_gateway_headers_and_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RECERTIA_OPENAI_HTTP_REFERER", "https://example.com")
    monkeypatch.setenv("RECERTIA_OPENROUTER_TITLE", "demo")
    monkeypatch.setenv("RECERTIA_OPENAI_EXTRA_BODY", '{"temperature":0.3}')
    captured: dict = {}

    def _fake_http(url: str, *, headers: dict, body: dict, timeout_s: float) -> dict:
        captured["headers"] = headers
        captured["body"] = body
        return {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    with patch("recertia.solver.providers._http_json", side_effect=_fake_http):
        client = OpenAIModelClient(
            api_key="sk-or-test",
            model_id="moonshotai/kimi-k2",
            base_url="https://openrouter.ai/api/v1/chat/completions",
        )
        client.complete("hi")

    assert captured["headers"]["authorization"] == "Bearer sk-or-test"
    assert captured["headers"]["HTTP-Referer"] == "https://example.com"
    assert captured["headers"]["X-OpenRouter-Title"] == "demo"
    assert captured["body"]["model"] == "moonshotai/kimi-k2"
    assert captured["body"]["temperature"] == 0.3
    assert captured["body"]["messages"][-1]["content"] == "hi"


def test_openai_extra_headers_must_be_object(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECERTIA_OPENAI_EXTRA_HEADERS", "[1,2]")
    with pytest.raises(ProviderError, match="JSON object"):
        openai_compat_headers()


def test_og7_unknown_slug_is_not_vendor_exact() -> None:
    assert cost_is_vendor_exact(provider="openai", model_id="moonshotai/kimi-k2") is False
    assert cost_is_vendor_exact(provider="openai", model_id="gpt-4o") is True
    cost = estimate_cost_usd(
        provider="openai",
        model_id="moonshotai/kimi-k2",
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
    )
    # unknown slug uses default 1.0 + 3.0 USD / MTok, not a vendor table
    assert cost == pytest.approx(4.0)
    assert "vendor-exact" not in str(cost)


def test_og8_max_tokens_env_fills_when_extra_body_omits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RECERTIA_OPENAI_EXTRA_BODY", raising=False)
    monkeypatch.setenv("RECERTIA_OPENAI_MAX_TOKENS", "128")
    assert openai_compat_extra_body()["max_tokens"] == 128
    monkeypatch.setenv("RECERTIA_OPENAI_EXTRA_BODY", '{"max_tokens":9}')
    assert openai_compat_extra_body()["max_tokens"] == 9


def test_og9_openrouter_error_json_includes_gateway_code() -> None:
    import io
    import json
    import urllib.error
    from email.message import Message

    payload = json.dumps(
        {"error": {"message": "Provider returned error", "code": "insufficient_quota"}}
    ).encode()

    class _GatewayHTTPError(urllib.error.HTTPError):
        def __init__(self) -> None:
            super().__init__(
                "https://openrouter.ai/api/v1/chat/completions",
                400,
                "Bad Request",
                hdrs=Message(),
                fp=io.BytesIO(payload),
            )
            self._payload = payload

        def read(self, n: int = -1) -> bytes:  # noqa: ARG002
            return self._payload

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise _GatewayHTTPError()

    with patch("urllib.request.urlopen", side_effect=_raise):
        client = OpenAIModelClient(
            api_key="sk-or-test",
            model_id="moonshotai/kimi-k2",
            base_url="https://openrouter.ai/api/v1/chat/completions",
        )
        with pytest.raises(ProviderError, match=r"code=insufficient_quota"):
            client.complete("hi")


def test_og10_concatenates_list_text_content_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del monkeypatch

    def _fake_http(url: str, *, headers: dict, body: dict, timeout_s: float) -> dict:
        del url, headers, body, timeout_s
        return {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": "hel"},
                            {"type": "image_url", "image_url": {"url": "x"}},
                            {"type": "text", "text": "lo"},
                        ]
                    }
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    with patch("recertia.solver.providers._http_json", side_effect=_fake_http):
        client = OpenAIModelClient(api_key="sk-test", model_id="gpt-4o-mini")
        response = client.complete("hi")
    assert response.text == "hello"


def test_og11_unknown_slug_returns_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from recertia.api import create_app

    monkeypatch.setenv("RECERTIA_MODEL_ALLOWLIST", "openai:moonshotai/kimi-k2")
    monkeypatch.delenv("RECERTIA_MODEL_ALLOWLIST_PATH", raising=False)
    app = create_app(root=tmp_path / "api-root", skills_root=tmp_path / "skills")
    issued = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    client = TestClient(app)
    headers = {"X-API-Key": issued.secret}

    listed = client.get("/v1/models", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["models"] == ["openai:moonshotai/kimi-k2"]

    denied = client.post(
        "/v1/runs",
        json={
            "request": "x",
            "run_id": "bad-model",
            "model": "openai:evil/not-allowed",
            "budget": {"max_attempts": 1},
        },
        headers=headers,
    )
    assert denied.status_code == 400, denied.text
    body = denied.json()
    assert body["error"]["code"] == "model_not_allowed"
    assert "allowlist" in body["error"]["message"].lower()


def test_og11_empty_allowlist_rejects_console_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from recertia.api import create_app

    monkeypatch.delenv("RECERTIA_MODEL_ALLOWLIST", raising=False)
    monkeypatch.delenv("RECERTIA_MODEL_ALLOWLIST_PATH", raising=False)
    app = create_app(root=tmp_path / "api-root", skills_root=tmp_path / "skills")
    issued = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    client = TestClient(app)
    denied = client.post(
        "/v1/runs",
        json={"request": "x", "model": "openai:moonshotai/kimi-k2"},
        headers={"X-API-Key": issued.secret},
    )
    assert denied.status_code == 400
    assert denied.json()["error"]["code"] == "model_not_allowed"


def test_og11_allowlist_not_in_console_static() -> None:
    from pathlib import Path

    from recertia.solver.model_allowlist import canonical_model_ref, model_is_allowed

    static = Path(__file__).resolve().parents[2] / "console" / "static"
    assert static.is_dir()
    forbidden = ("RECERTIA_MODEL_ALLOWLIST", "MODEL_ALLOWLIST", "openai:moonshotai/")
    for path in static.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for token in forbidden:
            assert token not in text, f"{path} must not ship {token}"

    allowed = ("openai:moonshotai/kimi-k2",)
    assert model_is_allowed("openai:moonshotai/kimi-k2", allowed)
    assert model_is_allowed("moonshotai/kimi-k2", allowed)
    assert not model_is_allowed("openai:evil/slug", allowed)
    assert canonical_model_ref("openai-compat:org/m") == "openai:org/m"
