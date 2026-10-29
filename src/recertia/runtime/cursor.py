"""Opaque keyset cursor for list endpoints (issue #136).

``encode_cursor`` and ``decode_cursor`` follow the codec in the REL-6 spec: the cursor is
the URL-safe base64 (no padding) of canonical JSON. Decoding is strict. A cursor is
rejected when it is longer than ``MAX_CURSOR_CHARS``, is not base64 or JSON, is not a flat
object with exactly the expected keys and string-or-null values, or is not the canonical
encoding of what it decodes to. Every rejection is an ``InvalidCursor`` with a fixed
message that does not echo the input.

The cursor is not signed and not bound to a tenant. It carries only a position; every
read is filtered by tenant, so a cursor taken from another tenant's list is still safe.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import NamedTuple

from recertia.runtime.errors import InvalidCursor

MAX_CURSOR_CHARS = 512
MAX_RUN_ID_CHARS = 256

_RUN_KEYS = frozenset({"created_at", "run_id"})


def _invalid() -> InvalidCursor:
    return InvalidCursor("invalid cursor")


def encode_cursor(payload: Mapping[str, object]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, *, expected_keys: frozenset[str] | set[str]) -> dict[str, str | None]:
    """Return the payload of ``cursor``. Raises ``InvalidCursor`` on anything but a canonical cursor."""

    if not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_CHARS:
        raise _invalid()
    try:
        padded = cursor.encode("ascii") + b"=" * (-len(cursor) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        data = json.loads(raw.decode("ascii"))
    except (ValueError, RecursionError) as exc:
        raise _invalid() from exc
    if not isinstance(data, dict) or set(data) != set(expected_keys):
        raise _invalid()
    if any(value is not None and not isinstance(value, str) for value in data.values()):
        raise _invalid()
    if encode_cursor(data) != cursor:
        raise _invalid()
    return data


class RunPosition(NamedTuple):
    """Where a run list stopped: the last row's UTC ``created_at`` (``None`` if undated) and run id."""

    created_at: datetime | None
    run_id: str


def encode_run_cursor(position: RunPosition) -> str:
    created = position.created_at.astimezone(UTC).isoformat() if position.created_at is not None else None
    return encode_cursor({"created_at": created, "run_id": position.run_id})


def decode_run_cursor(cursor: str) -> RunPosition:
    data = decode_cursor(cursor, expected_keys=_RUN_KEYS)
    run_id = data["run_id"]
    if not run_id or len(run_id) > MAX_RUN_ID_CHARS:
        raise _invalid()
    raw = data["created_at"]
    if raw is None:
        return RunPosition(None, run_id)
    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            raise _invalid()
        return RunPosition(parsed.astimezone(UTC), run_id)
    except (ValueError, OverflowError) as exc:
        raise _invalid() from exc
