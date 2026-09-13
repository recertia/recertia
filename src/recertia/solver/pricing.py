"""Token → USD pricing for provider cost accounting (operator-mode GA P0-1).

Rates are approximate public list prices and can be overridden via
``RECERTIA_MODEL_PRICE_<PROVIDER>_<MODEL>_IN`` / ``_OUT`` (USD per 1M tokens)
or a blanket ``RECERTIA_DEFAULT_INPUT_USD_PER_MTOK`` / ``RECERTIA_DEFAULT_OUTPUT_USD_PER_MTOK``.
"""

from __future__ import annotations

import os
import re

# Approximate USD per 1M tokens: (input, output). Keys are lowercase model-id prefixes.
_DEFAULT_RATES: dict[str, tuple[float, float]] = {
    "claude-opus": (15.0, 75.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku": (0.80, 4.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-3-5-haiku": (0.80, 4.0),
    "claude-3-opus": (15.0, 75.0),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.0),
    "gpt-4-turbo": (10.0, 30.0),
    "gpt-4": (30.0, 60.0),
    "o3-mini": (1.10, 4.40),
    "o1-mini": (1.10, 4.40),
    "o1": (15.0, 60.0),
}


def _env_rate(provider: str, model_id: str, kind: str) -> float | None:
    slug = re.sub(r"[^a-z0-9]+", "_", f"{provider}_{model_id}".lower()).strip("_")
    key = f"RECERTIA_MODEL_PRICE_{slug}_{kind}".upper()
    raw = os.environ.get(key, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _lookup_table(model_id: str) -> tuple[float, float] | None:
    mid = model_id.lower()
    # Longest prefix match.
    best: tuple[float, float] | None = None
    best_len = -1
    for prefix, rates in _DEFAULT_RATES.items():
        if mid.startswith(prefix) and len(prefix) > best_len:
            best = rates
            best_len = len(prefix)
    return best


def estimate_cost_usd(
    *,
    provider: str,
    model_id: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """Estimate USD cost from token counts. Never returns negative."""

    in_rate = _env_rate(provider, model_id, "IN")
    out_rate = _env_rate(provider, model_id, "OUT")
    if in_rate is None or out_rate is None:
        table = _lookup_table(model_id)
        if table is None:
            in_rate = float(os.environ.get("RECERTIA_DEFAULT_INPUT_USD_PER_MTOK", "1.0"))
            out_rate = float(os.environ.get("RECERTIA_DEFAULT_OUTPUT_USD_PER_MTOK", "3.0"))
        else:
            if in_rate is None:
                in_rate = table[0]
            if out_rate is None:
                out_rate = table[1]
    cost = (prompt_tokens / 1_000_000.0) * in_rate + (completion_tokens / 1_000_000.0) * out_rate
    return max(0.0, cost)


def cost_is_vendor_exact(*, provider: str, model_id: str) -> bool:
    """True only when a built-in table rate matched (not blanket defaults).

    Gateway slugs without a table match MUST NOT be treated as vendor-exact spend
    (remaining-work OG-7). Operator env overrides are still operator-supplied, not
    vendor invoices.
    """

    if _env_rate(provider, model_id, "IN") is not None or _env_rate(provider, model_id, "OUT") is not None:
        return False
    return _lookup_table(model_id) is not None
