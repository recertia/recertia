"""REC-056-nits C5: ``reserve`` reads the clock only after ``BEGIN IMMEDIATE``.

``windows``-marked so the real-Windows CI job runs it too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from recertia.persistence.idempotency import IdempotencyStore, KeyScope, Outcome

pytestmark = pytest.mark.windows


def test_reserve_reads_the_clock_after_begin_immediate(tmp_path: Path) -> None:
    events: list[str] = []

    def clock() -> int:
        events.append("clock")
        return 1_000_000

    store = IdempotencyStore(tmp_path / "control.db", clock=clock)
    try:
        store._writer().set_trace_callback(lambda sql: events.append(sql.strip().upper()))
        events.clear()

        reservation = store.reserve(KeyScope("t1", "p1", "s1", "k1"), "h1")

        assert reservation.outcome is Outcome.RESERVED
        assert "BEGIN IMMEDIATE" in events and "clock" in events, events
        assert events.index("BEGIN IMMEDIATE") < events.index("clock"), events
    finally:
        store.close()
