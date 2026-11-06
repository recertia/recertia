"""Server-side model slug allowlist for console-selected ``POST /v1/runs`` (OR3 / OG-11).

The allowlist is process configuration (``RECERTIA_MODEL_ALLOWLIST`` and/or
``RECERTIA_MODEL_ALLOWLIST_PATH``). It MUST NOT be shipped in package ``console/static/``.
Omitting ``model`` on a run keeps process-level env (``RECERTIA_MODEL_PROVIDER`` /
``RECERTIA_MODEL_ID``). A present ``model`` is fail-closed: empty allowlist or an
unknown slug is rejected.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from recertia.config import ModelConfig, load_model_config

_KNOWN_PROVIDERS = frozenset({"openai", "openai-compat", "anthropic", "stub"})


class ModelAllowlistError(ValueError):
    """Console-selected model is missing from the server allowlist."""

    def __init__(self, message: str, *, code: str = "model_not_allowed") -> None:
        super().__init__(message)
        self.code = code


def normalize_model_ref(raw: str) -> tuple[str, str]:
    """Return ``(provider, slug)``. Provider is empty when the input is slug-only."""

    text = raw.strip()
    if not text:
        return "", ""
    if ":" in text:
        left, _, right = text.partition(":")
        left_l = left.strip().lower()
        if left_l in _KNOWN_PROVIDERS:
            provider = "openai" if left_l == "openai-compat" else left_l
            return provider, right.strip()
    return "", text


def canonical_model_ref(raw: str) -> str:
    provider, slug = normalize_model_ref(raw)
    if provider and slug:
        return f"{provider}:{slug}"
    return slug or raw.strip()


def _load_allowlist_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("["):
        payload = json.loads(stripped)
        if not isinstance(payload, list):
            raise ValueError("allowlist JSON must be a list of strings")
        return [str(item).strip() for item in payload if str(item).strip()]
    items: list[str] = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        items.append(line)
    return items


def load_model_allowlist(*, environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Load ``provider:slug`` entries from env and optional file. Order-preserving unique."""

    env = os.environ if environ is None else environ
    items: list[str] = []
    raw = str(env.get("RECERTIA_MODEL_ALLOWLIST") or "").strip()
    if raw:
        items.extend(part.strip() for part in raw.split(",") if part.strip())
    path_raw = str(env.get("RECERTIA_MODEL_ALLOWLIST_PATH") or "").strip()
    if path_raw:
        items.extend(_load_allowlist_file(Path(path_raw)))
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = canonical_model_ref(item)
        if not key or key in seen:
            continue
        seen.add(key)
        provider, slug = normalize_model_ref(item)
        out.append(f"{provider}:{slug}" if provider else slug)
    return tuple(out)


def model_is_allowed(model: str, allowlist: tuple[str, ...] | None = None) -> bool:
    allowed = load_model_allowlist() if allowlist is None else allowlist
    if not allowed:
        return False
    cand_p, cand_s = normalize_model_ref(model)
    if not cand_s:
        return False
    for item in allowed:
        item_p, item_s = normalize_model_ref(item)
        if cand_s != item_s:
            continue
        if cand_p and item_p and cand_p != item_p:
            continue
        return True
    return False


def ensure_model_allowed(model: str, *, allowlist: tuple[str, ...] | None = None) -> None:
    allowed = load_model_allowlist() if allowlist is None else allowlist
    if not allowed:
        raise ModelAllowlistError(
            "model is not on the server allowlist (allowlist empty; set "
            "RECERTIA_MODEL_ALLOWLIST or RECERTIA_MODEL_ALLOWLIST_PATH)"
        )
    if not model_is_allowed(model, allowed):
        raise ModelAllowlistError(f"model {model!r} is not on the server allowlist")


def config_for_allowed_model(model: str) -> ModelConfig:
    """Build ``ModelConfig`` for a console-selected slug (caller already allowlisted it)."""

    provider, slug = normalize_model_ref(model)
    if not slug:
        raise ValueError("model slug is empty")
    if not provider:
        env_p = os.environ.get("RECERTIA_MODEL_PROVIDER", "openai").strip().lower()
        if env_p in {"", "stub", "none"}:
            env_p = "openai"
        if env_p == "openai-compat":
            env_p = "openai"
        provider = env_p
    return load_model_config(model=f"{provider}:{slug}")
