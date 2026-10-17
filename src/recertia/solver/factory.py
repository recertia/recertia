"""Construct :class:`ModelClient` instances from :class:`~recertia.config.ModelConfig`."""

from __future__ import annotations

from dataclasses import replace

from recertia.config import ModelConfig, load_model_config, resolve_api_key
from recertia.solver.model import ModelClient, StubModelClient
from recertia.solver.providers import AnthropicModelClient, OpenAIModelClient


class ModelConfigError(ValueError):
    """Invalid or incomplete model configuration for the requested provider."""


def build_model_client(
    config: ModelConfig | None = None,
    *,
    role: str = "solver",
    model_id: str | None = None,
    tenant: str | None = None,
) -> ModelClient:
    """Build a model client for ``role`` (solver or verifier).

    Raises :class:`ModelConfigError` when a non-stub provider is missing credentials
    or a model id.
    """

    cfg = config or load_model_config()
    if model_id is not None:
        cfg = replace(cfg, model_id=model_id)

    if cfg.provider == "stub":
        return StubModelClient(
            provider="stub",
            model_id=cfg.model_id or "stub",
            role=role,
            max_retries=cfg.max_retries,
            timeout_s=cfg.timeout_s,
        )

    if not cfg.model_id:
        raise ModelConfigError(
            f"provider {cfg.provider!r} requires a model id "
            f"(RECERTIA_MODEL_ID or --model {cfg.provider}:<id>)"
        )
    api_key = resolve_api_key(cfg, tenant=tenant)
    if not api_key:
        env = cfg.api_key_env or "(unset)"
        raise ModelConfigError(
            f"provider {cfg.provider!r} requires an API key in ${env}"
        )

    base_url_env = {
        "anthropic": "RECERTIA_ANTHROPIC_BASE_URL",
        "openai": "RECERTIA_OPENAI_BASE_URL",
    }.get(cfg.provider)
    from recertia.settings import load_settings

    settings = load_settings()
    base_url = ""
    if base_url_env == "RECERTIA_ANTHROPIC_BASE_URL":
        base_url = settings.anthropic_base_url or ""
    elif base_url_env == "RECERTIA_OPENAI_BASE_URL":
        base_url = settings.openai_base_url or ""

    if cfg.provider == "anthropic":
        kwargs: dict = {
            "api_key": api_key,
            "model_id": cfg.model_id,
            "max_retries": cfg.max_retries,
            "timeout_s": cfg.timeout_s,
            "role": role,
            "credential_id": cfg.api_key_env,
        }
        if base_url:
            kwargs["base_url"] = base_url
        return AnthropicModelClient(**kwargs)

    if cfg.provider == "openai":
        kwargs = {
            "api_key": api_key,
            "model_id": cfg.model_id,
            "max_retries": cfg.max_retries,
            "timeout_s": cfg.timeout_s,
            "role": role,
            "credential_id": cfg.api_key_env,
        }
        if base_url:
            kwargs["base_url"] = base_url
        return OpenAIModelClient(**kwargs)

    raise ModelConfigError(f"unsupported provider {cfg.provider!r}")


def build_solver_and_verifier(
    config: ModelConfig | None = None,
) -> tuple[ModelClient | None, ModelClient | None]:
    """Return ``(solver, verifier)`` according to config and stub policy.

    Stub provider leaves both as ``None`` unless ``RECERTIA_ALLOW_STUB_MODEL=1``,
    so scratch solving fails loud instead of silently running a no-op.
    """

    cfg = config or load_model_config()
    from recertia.settings import load_settings

    allow_stub = load_settings().allow_stub_model
    if cfg.provider == "stub" and not allow_stub:
        return None, None

    solver = build_model_client(cfg, role="solver")
    verifier: ModelClient | None = None
    if cfg.verifier_model_id:
        verifier = build_model_client(cfg, role="verifier", model_id=cfg.verifier_model_id)
    return solver, verifier
