"""Empty eval DB -> no invented numbers, no counted soak week (weekly-ops pipeline, per PR).

Runs the same entry points as .github/workflows/weekly-ops.yml (scripts/weekly_metrics_report.py
and recertia.ops.soak.classify_week) against an empty DB, so a regression shows up on the PR
instead of as a silently "green" Monday soak week.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from recertia.ops.soak import EMPTY_EVAL, classify_week

REPO = Path(__file__).resolve().parents[2]
OBSERVATION_DERIVED = (
    "reuse_rate",
    "first_attempt_success",
    "attempts_to_success",
    "cost_per_solved_task",
)


def _empty_report(tmp_path: Path) -> dict:
    out = tmp_path / "weekly-metrics.json"
    subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts/weekly_metrics_report.py"),
            "--eval-db",
            str(tmp_path / "evals.db"),
            "--skills-root",
            str(REPO / "skills"),
            "--canary-root",
            str(REPO / "evals/canary/planted-failure"),
            "--output",
            str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=120,
    )
    return json.loads(out.read_text())


def test_empty_db_reports_nulls_not_zeros(tmp_path: Path) -> None:
    payload = _empty_report(tmp_path)
    r = payload["report"]
    for f in OBSERVATION_DERIVED:
        assert r[f] is None, f"{f} invented from zero observations: {r[f]!r}"
    lift = r["causal_lift"]
    assert lift["treatment"]["trials"] == 0 and lift["control"]["trials"] == 0
    assert lift["estimate"] is None and lift["interval"] is None
    assert lift["status"] == "insufficient_data"
    assert payload["claim"] != "established"
    # every null metric with a recorded reason stays null (no number + 'unavailable' at once)
    for f in r.get("unavailable", {}):
        assert r.get(f) is None, f"{f} has both a value and an unavailable reason"


def test_empty_week_is_recorded_not_counted(tmp_path: Path) -> None:
    week = classify_week(_empty_report(tmp_path))
    assert week.counted is False
    assert week.observation_count == 0
    assert week.reason == EMPTY_EVAL
    assert week.observation_count == 0
    assert week.as_dict()["ga_claimed"] is False


def test_zero_trials_never_counts_as_an_observation() -> None:
    """A summary value with zero recorded trials must not count as a soak week (bug 8:
    observation_count() used to floor at 1 whenever first_attempt_success was set)."""
    week = classify_week(
        {
            "report": {
                "first_attempt_success": 0.0,
                "causal_lift": {"treatment": {"trials": 0}, "control": {"trials": 0}},
            }
        }
    )
    assert week.counted is False
    assert week.observation_count == 0
