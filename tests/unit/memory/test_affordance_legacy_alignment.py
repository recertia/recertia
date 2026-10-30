"""REC-056-nits C2: a legacy import pairs durations and costs from the newest end."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from recertia.memory.affordance import AffordanceStore


def _legacy(path: Path, durations: list[float], costs: list[float]) -> None:
    tool = {
        "tool": "t",
        "invocations": len(durations),
        "failures": 0,
        "durations_s": durations,
        "costs_usd": costs,
        "error_signatures": {},
    }
    path.write_text(json.dumps({"tools": {"t": tool}, "resources": {}}), encoding="utf-8")


def _samples(db: Path) -> list[tuple[float, float]]:
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT duration_s, cost_usd FROM affordance_tool_samples WHERE tool='t' ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()
    return [(float(d), float(c)) for d, c in rows]


@pytest.mark.parametrize(
    ("durations", "costs", "expected"),
    [
        ([1.0, 2.0, 3.0, 4.0, 5.0], [0.3, 0.4, 0.5], [(3.0, 0.3), (4.0, 0.4), (5.0, 0.5)]),
        ([4.0, 5.0], [0.1, 0.2, 0.4, 0.5], [(4.0, 0.4), (5.0, 0.5)]),
    ],
)
def test_import_keeps_the_newest_aligned_pairs(
    tmp_path: Path, durations: list[float], costs: list[float], expected: list[tuple[float, float]]
) -> None:
    path = tmp_path / "affordances.json"
    _legacy(path, durations, costs)
    store = AffordanceStore(path)
    try:
        store.save()
    finally:
        store.close()
    assert _samples(tmp_path / "runtime.db") == expected
