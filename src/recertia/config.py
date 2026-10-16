"""Runtime configuration: model provider selection from env / CLI."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

ProviderName = Literal["stub", "anthropic", "openai"]


@dataclass(frozen=True)
class ModelConfig:
    """How to construct solver (and optional verifier) model clients."""

    provider: ProviderName = "stub"
    model_id: str = "stub"
    api_key_env: str = ""
    verifier_model_id: str | None = None
    timeout_s: float = 60.0
    max_retries: int = 2


def _parse_provider(raw: str | None) -> ProviderName:
    if not raw or raw.strip().lower() in {"", "stub", "none"}:
        return "stub"
    name = raw.strip().lower()
    if name in {"anthropic", "openai", "stub"}:
        return name  # type: ignore[return-value]
    # Allow "anthropic:claude-…" shorthand
    if ":" in name:
        left, _, right = name.partition(":")
        if left in {"anthropic", "openai", "stub"}:
            return left  # type: ignore[return-value]
    raise ValueError(
        f"unsupported model provider {raw!r}; expected stub|anthropic|openai "
        f"or provider:model_id"
    )


def _split_provider_model(raw: str | None) -> tuple[ProviderName, str | None]:
    if not raw:
        return "stub", None
    text = raw.strip()
    if ":" in text:
        left, _, right = text.partition(":")
        provider = _parse_provider(left)
        return provider, right or None
    return _parse_provider(text), None


def load_model_config(
    *,
    model: str | None = None,
    verifier: str | None = None,
) -> ModelConfig:
    """Load model config from CLI overrides and environment.

    Precedence: explicit ``model`` / ``verifier`` args →
    ``RECERTIA_MODEL_PROVIDER`` / ``RECERTIA_MODEL_ID`` / ``RECERTIA_VERIFIER_MODEL_ID``.
    """

    env_provider = os.environ.get("RECERTIA_MODEL_PROVIDER")
    env_model_id = os.environ.get("RECERTIA_MODEL_ID")
    env_verifier = os.environ.get("RECERTIA_VERIFIER_MODEL_ID")

    if model:
        provider, model_id = _split_provider_model(model)
        if model_id is None:
            model_id = env_model_id or ("stub" if provider == "stub" else "")
    else:
        provider = _parse_provider(env_provider)
        model_id = env_model_id or ("stub" if provider == "stub" else "")

    if not model_id and provider != "stub":
        raise ValueError(
            f"provider {provider!r} requires a model id "
            f"(RECERTIA_MODEL_ID or --model {provider}:<id>)"
        )

    verifier_model_id: str | None = None
    if verifier:
        v_provider, v_id = _split_provider_model(verifier)
        if v_provider != provider and v_provider != "stub":
            # Same provider family expected for single-user; allow stub verifier in tests.
            pass
        verifier_model_id = v_id or env_verifier
    else:
        verifier_model_id = env_verifier

    from recertia.settings import load_settings

    settings = load_settings()
    api_key_env = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "stub": "",
    }[provider]
    if settings.api_key_env:
        api_key_env = settings.api_key_env

    timeout_s = settings.model_timeout_s
    max_retries = settings.model_max_retries
    return ModelConfig(
        provider=provider,
        model_id=model_id or "stub",
        api_key_env=api_key_env,
        verifier_model_id=verifier_model_id,
        timeout_s=timeout_s,
        max_retries=max_retries,
    )


def resolve_api_key(config: ModelConfig) -> str | None:
    """Return the API key from the configured env var, or None for stub."""

    if config.provider == "stub" or not config.api_key_env:
        return None
    key = os.environ.get(config.api_key_env, "").strip()
    return key or None
