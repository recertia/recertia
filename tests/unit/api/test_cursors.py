"""REL-6 keyset cursors."""

from __future__ import annotations

from pathlib import Path

import pytest

from recertia.runtime.cursor import decode_cursor, encode_cursor
from recertia.runtime.errors import InvalidCursor


def test_cases_facts_proposals_round_trip(tmp_path: Path) -> None:
    del tmp_path
    cursor = encode_cursor({"created_at": "2026-01-01T00:00:00+00:00", "run_id": "r1"})
    decoded = decode_cursor(cursor, expected_keys={"created_at", "run_id"})
    assert decoded["run_id"] == "r1"
    assert decoded["created_at"] == "2026-01-01T00:00:00+00:00"
    with pytest.raises(InvalidCursor):
        decode_cursor("not-a-cursor", expected_keys={"created_at", "run_id"})
    with pytest.raises(InvalidCursor):
        decode_cursor(
            encode_cursor({"run_id": "r1"}),
            expected_keys={"created_at", "run_id"},
        )
