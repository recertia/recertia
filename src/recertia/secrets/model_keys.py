"""Bring-your-own provider keys, one map for every tenant.

The map is a JSON object in ``RECERTIA_TENANT_MODEL_KEYS`` or a mode-0600 file
named by ``RECERTIA_TENANT_MODEL_KEYS_FILE``. The executor child environment
never receives that map or another tenant's provider key.
"""

from __future__ import annotations

import json
import stat
from collections.abc import Mapping
from pathlib import Path

from recertia.errors import ConfigError
from recertia.ports.identity import tenant_id

_PROVIDERS = frozenset({"anthropic", "openai"})
_SECRET_ENV_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "RECERTIA_TENANT_MODEL_KEYS",
        "RECERTIA_TENANT_MODEL_KEYS_FILE",
        "RECERTIA_LEDGER_HMAC_KEY",
        "RECERTIA_API_KEY_PEPPER",
        "RECERTIA_CONSOLE_SESSION_SECRET",
        "RECERTIA_OIDC_CLIENT_SECRET",
    }
)


def is_executor_secret(key: str) -> bool:
    """True for provider credentials and the tenant key map itself."""

    if key in _SECRET_ENV_KEYS or key.startswith("RECERTIA_TENANT_"):
        return True
    upper = key.upper()
    return upper.endswith("_API_KEY") or upper.endswith("_API_SECRET")


def tenant_key_map() -> dict[str, dict[str, str]] | None:
    """Return the configured map, or None when the operator has not set one."""

    from recertia.settings import load_settings

    settings = load_settings()
    file_path = settings.tenant_model_keys_file.strip()
    if file_path:
        return _parse_map(_read_key_file(Path(file_path)))
    raw = settings.tenant_model_keys.strip()
    if not raw:
        return None
    return _parse_map(raw)


def tenant_provider_key(tenant: str | None, provider: str) -> tuple[str | None, bool]:
    """Return ``(key, authoritative)``.

    Authoritative means the process environment must not fill the gap. A
    configured map never substitutes another tenant's secret.
    """

    mapping = tenant_key_map()
    if not tenant:
        return None, False
    name = str(tenant_id(tenant))
    if mapping is not None:
        return mapping.get(name, {}).get(provider) or None, True
    from recertia.settings import load_settings

    if load_settings().deployment_mode == "multi_node":
        return None, True
    return None, False


def scrub_executor_env(source: Mapping[str, str], allowed: tuple[str, ...]) -> dict[str, str]:
    """Copy the allowlist and drop every provider or tenant secret."""

    allowed_set = set(allowed)
    return {
        key: value
        for key, value in source.items()
        if key in allowed_set and key != "HOME" and not is_executor_secret(key)
    }


def _read_key_file(path: Path) -> str:
    if not path.is_file():
        raise ConfigError(f"tenant model key file does not exist: {path}")
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(f"tenant model key file must be mode 0600: {path}")
    return path.read_text(encoding="utf-8")


def _parse_map(raw: str) -> dict[str, dict[str, str]]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError("tenant model key map is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ConfigError("tenant model key map must be a JSON object")
    parsed: dict[str, dict[str, str]] = {}
    for tenant, providers in data.items():
        if not isinstance(tenant, str) or not isinstance(providers, dict):
            raise ConfigError("tenant model key map values must be objects")
        name = str(tenant_id(tenant))
        bucket: dict[str, str] = {}
        for provider, secret in providers.items():
            if provider not in _PROVIDERS:
                raise ConfigError(f"unsupported provider in tenant model key map: {provider}")
            if not isinstance(secret, str) or not secret.strip():
                raise ConfigError(f"empty provider key for tenant {name}")
            bucket[str(provider)] = secret.strip()
        parsed[name] = bucket
    return parsed
