#!/usr/bin/env python3
"""Read-only Terminal-RSI capacity baseline. Does not set flags or start a sandbox.

Writes ``docs/ops/rsi-capacity.json``. ``go`` is true only when the last 30 days
contain at least 50 distill candidates, every one of them has an elapsed time,
and ``pool_headroom`` is at least 1.5. Missing inputs stay null and ``go`` stays
false. This script never writes ``slo_waiver``.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

WINDOW_DAYS = 30
MIN_BATCH = 50
MIN_HEADROOM = 1.5
HISTORY_REL = Path("evals/golden/_promotion_logs")
OUTPUT_REL = Path("docs/ops/rsi-capacity.json")
_DURATION_KEYS = ("elapsed_s", "duration_s", "reexec_s")


def parse_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def is_candidate(record: dict) -> bool:
    skill = record.get("skill_id")
    run = record.get("run_id")
    return isinstance(skill, str) and bool(skill) and isinstance(run, str) and bool(run)


def duration_s(record: dict) -> float | None:
    for key in _DURATION_KEYS:
        value = record.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value >= 0:
            return float(value)
    return None


def median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def p95(values: list[float]) -> float:
    """Nearest rank. Index is ceil(0.95 * n), clamped to the sample."""
    ordered = sorted(values)
    rank = math.ceil(0.95 * len(ordered))
    rank = min(max(rank, 1), len(ordered))
    return ordered[rank - 1]


def pool_headroom(free_slots: int, batch_size: int, p95_s: float, slo_s: float) -> float | None:
    """Free slots divided by slots required to drain the batch inside the SLO.

    One slot finishes ``slo_s / p95_s`` candidates in the window, so the batch
    needs ``batch_size * p95_s / slo_s`` slots. Unusable inputs return None,
    never 0, so a missing pool is not reported as a measured empty pool.
    """
    if free_slots < 1 or batch_size < 1 or p95_s <= 0 or slo_s <= 0:
        return None
    needed = batch_size * p95_s / slo_s
    if needed <= 0:
        return None
    return free_slots / needed


def measure(
    records: list[dict],
    *,
    now: datetime,
    free_slots: int | None,
    distill_slo_s: float | None,
    history_records: int | None = None,
    slo_waiver: str | None = None,
) -> dict:
    """Capacity report. ``go`` ignores ``slo_waiver``; a waiver is not a measurement."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    start = now.astimezone(timezone.utc) - timedelta(days=WINDOW_DAYS)
    ignored = 0
    bad_stamp = 0
    outside = 0
    in_window: list[dict] = []
    for record in records:
        if not isinstance(record, dict) or not is_candidate(record):
            ignored += 1
            continue
        stamp = parse_at(record.get("at"))
        if stamp is None:
            bad_stamp += 1
            continue
        if stamp < start or stamp > now.astimezone(timezone.utc):
            outside += 1
            continue
        in_window.append(record)
    batch = len(in_window)
    durations = [elapsed for elapsed in (duration_s(row) for row in in_window) if elapsed is not None]
    missing_duration = batch - len(durations)
    timings_complete = batch > 0 and missing_duration == 0
    median_s = median(durations) if timings_complete else None
    p95_s = p95(durations) if timings_complete else None
    head = None
    if (
        timings_complete
        and p95_s is not None
        and free_slots is not None
        and distill_slo_s is not None
    ):
        head = pool_headroom(free_slots, batch, p95_s, distill_slo_s)
    go = batch >= MIN_BATCH and head is not None and head >= MIN_HEADROOM
    reasons: list[str] = []
    if batch < MIN_BATCH:
        reasons.append(f"batch_size {batch} < {MIN_BATCH}")
    if batch > 0 and not timings_complete:
        reasons.append("elapsed_s missing on one or more in-window candidates")
    if free_slots is None or distill_slo_s is None:
        reasons.append("no declared sandbox pool or distill SLO")
    elif head is None:
        reasons.append("pool_headroom uncomputable")
    elif head < MIN_HEADROOM:
        reasons.append(f"pool_headroom {head:.3f} < {MIN_HEADROOM}")
    waiver = slo_waiver.strip() if isinstance(slo_waiver, str) and slo_waiver.strip() else None
    seen = len(records) if history_records is None else history_records
    return {
        "measured_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "batch_size": batch,
        "reexec_fraction": (1.0 if batch else 0.0),
        "median_reexec_s": median_s,
        "p95_reexec_s": p95_s,
        "pool_headroom": head,
        "go": go,
        "slo_waiver": waiver,
        "measured": {
            "window_days": WINDOW_DAYS,
            "history_records": seen,
            "ignored": ignored,
            "dropped_bad_stamp": bad_stamp,
            "dropped_outside_window": outside,
            "durations_observed": len(durations),
            "pool_slots": free_slots,
            "distill_slo_s": distill_slo_s,
            "reason": "; ".join(reasons) if reasons else "go",
        },
    }


def load_history(directory: Path) -> tuple[list[dict], int]:
    """Load JSON objects from one directory. Invalid files count as ignored, not candidates."""
    if not directory.is_dir():
        return [], 0
    records: list[dict] = []
    seen = 0
    for path in sorted(directory.glob("*.json")):
        seen += 1
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            records.append({})
            continue
        records.append(payload if isinstance(payload, dict) else {})
    return records, seen


def existing_waiver(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    waiver = payload.get("slo_waiver")
    if isinstance(waiver, str) and waiver.strip():
        return waiver.strip()
    return None


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    records, seen = load_history(root / HISTORY_REL)
    out = root / OUTPUT_REL
    report = measure(
        records,
        now=datetime.now(timezone.utc),
        free_slots=None,
        distill_slo_s=None,
        history_records=seen,
        slo_waiver=existing_waiver(out),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("no-go" if not report["go"] else "go")
    print(report["measured"]["reason"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
