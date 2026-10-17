"""HTTP model providers (Anthropic Messages + OpenAI Chat Completions).

Uses the standard library only so the core package stays dependency-light. Optional
extras may wrap richer SDKs later; these clients are enough for single-user go-live.

OpenAI-compatible gateways (OpenRouter, etc.) can set optional headers / body fields via
``RECERTIA_OPENAI_HTTP_REFERER``, ``RECERTIA_OPENAI_TITLE``, ``RECERTIA_OPENAI_EXTRA_HEADERS``,
and ``RECERTIA_OPENAI_EXTRA_BODY``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from recertia.solver.model import ModelClient, ModelResponse


class ProviderError(RuntimeError):
    """Provider rejected the request or returned an unusable payload."""


class ProviderHTTPError(ProviderError):
    """HTTP failure with the status and ``Retry-After`` kept intact."""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        retry_after_s: float | None,
        body_excerpt: str,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after_s = retry_after_s
        self.body_excerpt = body_excerpt


def _retry_after_seconds(headers: object) -> float | None:
    if headers is None or not hasattr(headers, "get"):
        return None
    raw = headers.get("Retry-After")  # type: ignore[attr-defined]
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime

    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _http_json(
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout_s: float,
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        retry_after = _retry_after_seconds(exc.headers)
        gateway_code = None
        message = f"HTTP {exc.code} from {url}: {detail}"
        try:
            parsed_err = json.loads(detail)
            err = parsed_err.get("error") if isinstance(parsed_err, dict) else None
            if isinstance(err, dict):
                gateway_code = err.get("code")
                text = str(err.get("message") or detail)
                suffix = f" (code={gateway_code})" if gateway_code is not None else ""
                message = f"HTTP {exc.code} from {url}: {text}{suffix}"
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
        raise ProviderHTTPError(
            message,
            status=int(exc.code),
            retry_after_s=retry_after,
            body_excerpt=detail,
        ) from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"network error calling {url}: {exc}") from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ProviderError(f"non-JSON response from {url}") from exc
    if not isinstance(parsed, dict):
        raise ProviderError(f"unexpected JSON payload from {url}")
    return parsed


def openai_compat_headers() -> dict[str, str]:
    """Optional headers for OpenAI-compatible gateways (e.g. OpenRouter rankings)."""

    from recertia.settings import load_settings

    settings = load_settings()
    headers: dict[str, str] = {}
    referer = settings.openai_http_referer
    if referer:
        headers["HTTP-Referer"] = referer
    title = settings.openai_title or settings.openrouter_title
    if title:
        # OpenRouter accepts both; send the documented header plus the short alias.
        headers["X-OpenRouter-Title"] = title
        headers["X-Title"] = title
    extra_raw = settings.openai_extra_headers
    if extra_raw:
        try:
            extra = json.loads(extra_raw)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "RECERTIA_OPENAI_EXTRA_HEADERS must be a JSON object of string headers"
            ) from exc
        if not isinstance(extra, dict):
            raise ProviderError(
                "RECERTIA_OPENAI_EXTRA_HEADERS must be a JSON object of string headers"
            )
        for key, value in extra.items():
            headers[str(key)] = str(value)
    return headers


def openai_compat_extra_body() -> dict[str, Any]:
    """Optional Chat Completions body fields (temperature, provider prefs, …)."""

    from recertia.settings import load_settings

    settings = load_settings()
    raw = settings.openai_extra_body
    extra: dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderError("RECERTIA_OPENAI_EXTRA_BODY must be a JSON object") from exc
        if not isinstance(parsed, dict):
            raise ProviderError("RECERTIA_OPENAI_EXTRA_BODY must be a JSON object")
        blocked = {"model", "messages"}
        extra = {str(k): v for k, v in parsed.items() if str(k) not in blocked}
    max_tokens = settings.openai_max_tokens
    if max_tokens is not None and "max_tokens" not in extra:
        extra["max_tokens"] = max_tokens
    return extra


class AnthropicModelClient(ModelClient):
    """Anthropic Messages API client."""

    def __init__(
        self,
        *,
        api_key: str,
        model_id: str,
        max_retries: int = 2,
        timeout_s: float = 60.0,
        retry_backoff_s: float = 0.25,
        backoff_cap_s: float = 8.0,
        role: str = "solver",
        credential_id: str | None = None,
        base_url: str = "https://api.anthropic.com/v1/messages",
        max_tokens: int = 1024,
    ) -> None:
        super().__init__(
            max_retries=max_retries,
            timeout_s=timeout_s,
            retry_backoff_s=retry_backoff_s,
            backoff_cap_s=backoff_cap_s,
            provider="anthropic",
            model_id=model_id,
            role=role,
            credential_id=credential_id or "ANTHROPIC_API_KEY",
        )
        self._api_key = api_key
        self._base_url = base_url
        self._max_tokens = max_tokens

    def _complete_once(self, prompt: str, *, system: str | None) -> ModelResponse:
        body: dict[str, Any] = {
            "model": self.model_id,
            "max_tokens": self._max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system
        payload = _http_json(
            self._base_url,
            headers={
                "content-type": "application/json",
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
            },
            body=body,
            timeout_s=self._call_timeout_s,
        )
        text = _anthropic_text(payload)
        usage = payload.get("usage") or {}
        prompt_tokens = int(usage.get("input_tokens") or max(1, len(prompt) // 4))
        completion_tokens = int(usage.get("output_tokens") or max(1, len(text) // 4))
        from recertia.solver.pricing import estimate_cost_usd

        return ModelResponse(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=estimate_cost_usd(
                provider="anthropic",
                model_id=str(self.model_id or "anthropic"),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            ),
            model=str(self.model_id or "anthropic"),
        )


class OpenAIModelClient(ModelClient):
    """OpenAI-compatible Chat Completions client."""

    def __init__(
        self,
        *,
        api_key: str,
        model_id: str,
        max_retries: int = 2,
        timeout_s: float = 60.0,
        retry_backoff_s: float = 0.25,
        backoff_cap_s: float = 8.0,
        role: str = "solver",
        credential_id: str | None = None,
        base_url: str = "https://api.openai.com/v1/chat/completions",
    ) -> None:
        super().__init__(
            max_retries=max_retries,
            timeout_s=timeout_s,
            retry_backoff_s=retry_backoff_s,
            backoff_cap_s=backoff_cap_s,
            provider="openai",
            model_id=model_id,
            role=role,
            credential_id=credential_id or "OPENAI_API_KEY",
        )
        self._api_key = api_key
        self._base_url = base_url

    def _complete_once(self, prompt: str, *, system: str | None) -> ModelResponse:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body: dict[str, Any] = {"model": self.model_id, "messages": messages}
        body.update(openai_compat_extra_body())
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {self._api_key}",
            **openai_compat_headers(),
        }
        payload = _http_json(
            self._base_url,
            headers=headers,
            body=body,
            timeout_s=self._call_timeout_s,
        )
        text = _openai_text(payload)
        usage = payload.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or max(1, len(prompt) // 4))
        completion_tokens = int(usage.get("completion_tokens") or max(1, len(text) // 4))
        from recertia.solver.pricing import estimate_cost_usd

        return ModelResponse(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=estimate_cost_usd(
                provider="openai",
                model_id=str(self.model_id or "openai"),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            ),
            model=str(self.model_id or "openai"),
        )


def _anthropic_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, list) or not content:
        raise ProviderError("anthropic response missing content blocks")
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    text = "".join(parts).strip()
    if not text:
        raise ProviderError("anthropic response contained no text")
    return text


def _openai_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("error"), dict):
        err = payload["error"]
        code = err.get("code")
        message = str(err.get("message") or "openai error")
        suffix = f" (code={code})" if code is not None else ""
        raise ProviderError(f"{message}{suffix}")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderError("openai response missing choices")
    choice_message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(choice_message, dict):
        raise ProviderError("openai response missing message")
    content = choice_message.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                parts.append(part)
        text = "".join(parts).strip()
    else:
        text = str(content or "").strip()
    if not text:
        raise ProviderError("openai response contained no text")
    return text
