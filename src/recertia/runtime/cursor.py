"""Opaque keyset cursors for list endpoints (REL-6 / F21)."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any

from recertia.runtime.errors import InvalidCursor


def encode_cursor(payload: Mapping[str, object]) -> str:
    raw = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, *, expected_keys: set[str]) -> dict[str, Any]:
    text = (cursor or "").strip()
    if not text:
        raise InvalidCursor("invalid cursor")
    padded = text + ("=" * (-len(text) % 4))
    try:
        data = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        raise InvalidCursor("invalid cursor") from exc
    if not isinstance(data, dict) or set(data) != expected_keys:
        raise InvalidCursor("invalid cursor")
    return {str(key): data[key] for key in expected_keys}
