"""Opaque keyset cursor for GET /v1/runs (REC-056f).

The cursor is a base64-encoded JSON object with ``created_at`` and ``run_id``.
Old raw run_id cursors are rejected.
"""

from __future__ import annotations

import base64
import json

from recertia.runtime.errors import InvalidCursor


def encode_cursor(created_at: str, run_id: str) -> str:
    payload = json.dumps({"created_at": created_at, "run_id": run_id}, sort_keys=True)
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, str]:
    """Return ``(created_at, run_id)``. Raises ``InvalidCursor`` on any malformation."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode())
        data = json.loads(raw)
        created_at = str(data["created_at"])
        run_id = str(data["run_id"])
    except (KeyError, ValueError, json.JSONDecodeError, TypeError) as exc:
        raise InvalidCursor(f"invalid cursor: {cursor!r}") from exc
    if not created_at or not run_id:
        raise InvalidCursor(f"invalid cursor: {cursor!r}")
    return created_at, run_id
