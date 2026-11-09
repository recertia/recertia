"""Capacity-gate invariants. A missing pool or a short batch is not a go."""

from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "measure_rsi_capacity.py"
_SPEC = importlib.util.spec_from_file_location("measure_rsi_capacity", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_CAPACITY = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CAPACITY)

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def _row(i: int, *, at: datetime | None = None, elapsed: float | None = 10.0) -> dict:
    stamp = NOW if at is None else at
    row: dict[str, object] = {
        "skill_id": f"skill-{i}",
        "run_id": f"run-{i}",
        "at": stamp.isoformat(),
    }
    if elapsed is not None:
        row["elapsed_s"] = elapsed
    return row


class CapacitySuite:
    def test_empty_window_is_not_a_go(self) -> None:
        before = dict(os.environ)
        report = _CAPACITY.measure([], now=NOW, free_slots=None, distill_slo_s=None)
        self_assert_closed(report)
        assert report["batch_size"] == 0
        assert report["reexec_fraction"] == 0.0
        assert report["median_reexec_s"] is None
        assert report["p95_reexec_s"] is None
        assert report["pool_headroom"] is None
        assert report["observed_p95_reexec_s"] is None
        assert report["slo_waiver"] is None
        assert "elapsed_s missing" not in report["measured"]["reason"]
        assert os.environ == before

    def test_forty_nine_candidates_do_not_go(self) -> None:
        rows = [_row(i) for i in range(49)]
        report = _CAPACITY.measure(rows, now=NOW, free_slots=20, distill_slo_s=10_000.0)
        assert report["batch_size"] == 49
        assert report["go"] is False

    def test_headroom_below_one_point_five_does_not_go(self) -> None:
        rows = [_row(i, elapsed=10.0) for i in range(50)]
        report = _CAPACITY.measure(rows, now=NOW, free_slots=7, distill_slo_s=100.0)
        assert report["pool_headroom"] == 1.4
        assert report["go"] is False

    def test_exact_gate_goes(self) -> None:
        rows = [_row(i, elapsed=10.0) for i in range(50)]
        report = _CAPACITY.measure(rows, now=NOW, free_slots=8, distill_slo_s=100.0)
        floor = _CAPACITY.measure(
            [_row(i, elapsed=10.0) for i in range(50)],
            now=NOW,
            free_slots=15,
            distill_slo_s=50.0,
        )
        assert report["pool_headroom"] == 1.6
        assert report["go"] is True
        assert report["reexec_fraction"] == 1.0
        assert floor["pool_headroom"] == 1.5
        assert floor["go"] is True

    def test_missing_elapsed_is_not_zero_seconds(self) -> None:
        rows = [_row(i, elapsed=None) for i in range(50)]
        report = _CAPACITY.measure(rows, now=NOW, free_slots=100, distill_slo_s=100.0)
        assert report["median_reexec_s"] is None
        assert report["p95_reexec_s"] is None
        assert report["pool_headroom"] is None
        assert report["go"] is False

    def test_bool_elapsed_is_not_a_duration(self) -> None:
        row = _row(1)
        row["elapsed_s"] = True
        report = _CAPACITY.measure([row], now=NOW, free_slots=10, distill_slo_s=100.0)
        assert report["measured"]["durations_observed"] == 0
        assert report["go"] is False

    def test_records_older_than_thirty_days_are_outside_the_batch(self) -> None:
        old = _row(1, at=NOW - timedelta(days=31), elapsed=1.0)
        report = _CAPACITY.measure([old], now=NOW, free_slots=10, distill_slo_s=100.0)
        assert report["batch_size"] == 0
        assert report["measured"]["dropped_outside_window"] == 1
        assert report["go"] is False

    def test_waiver_does_not_flip_go(self) -> None:
        report = _CAPACITY.measure(
            [],
            now=NOW,
            free_slots=None,
            distill_slo_s=None,
            slo_waiver="distill SLO 2h, operator 2026-11-08",
        )
        assert report["go"] is False
        assert report["slo_waiver"] == "distill SLO 2h, operator 2026-11-08"

    def test_repo_promotion_logs_are_not_a_go(self) -> None:
        records, seen = _CAPACITY.load_history(_ROOT / "evals/golden/_promotion_logs")
        report = _CAPACITY.measure(
            records,
            now=NOW,
            free_slots=None,
            distill_slo_s=None,
            history_records=seen,
        )
        assert seen == 10
        assert report["batch_size"] == 0
        assert report["pool_headroom"] is None
        assert report["go"] is False
        assert report["slo_waiver"] is None

    def test_main_writes_only_the_report_and_keeps_a_waiver(self, tmp_path: Path) -> None:
        history = tmp_path / "evals" / "golden" / "_promotion_logs"
        history.mkdir(parents=True)
        (history / "one.json").write_text("{not json", encoding="utf-8")
        script = tmp_path / "scripts"
        script.mkdir()
        source = _SCRIPT.read_text(encoding="utf-8")
        (script / "measure_rsi_capacity.py").write_text(source, encoding="utf-8")
        out = tmp_path / "docs" / "ops" / "rsi-capacity.json"
        out.parent.mkdir(parents=True)
        out.write_text(json.dumps({"slo_waiver": "keep me"}) + "\n", encoding="utf-8")
        before = dict(os.environ)
        spec = importlib.util.spec_from_file_location("measure_copy", script / "measure_rsi_capacity.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.main() == 0
        assert os.environ == before
        written = json.loads(out.read_text(encoding="utf-8"))
        assert written["go"] is False
        assert written["slo_waiver"] == "keep me"
        assert written["pool_headroom"] is None
        assert written["observed_p95_reexec_s"] is None
        assert (history / "one.json").read_text(encoding="utf-8") == "{not json"
        assert list(p for p in (tmp_path / "docs").rglob("*") if p.is_file()) == [out]


def self_assert_closed(report: dict) -> None:
    assert report["go"] is False


# pytest collects test_* functions and TestCase classes. Bind the methods.
class TestCapacity(CapacitySuite):
    pass
