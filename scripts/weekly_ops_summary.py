#!/usr/bin/env python3
"""Aggregate-only weekly-ops summary for ``$GITHUB_STEP_SUMMARY`` (REC-022).

weekly-ops.yml used to upload its eval output (metrics, probes, canary, soak log) as a
public artifact on a public repo. It now writes only this summary. Each value comes from an
explicit allowlist of aggregate fields. Numbers, booleans, and null are printed as they are.
A string is printed only if it is an enum-like token (``^[a-z0-9_.-]{1,40}$``, such as a
status). Anything else is printed as ``(omitted)``. No per-probe, per-fixture, or per-run rows.

Usage:
  python3 scripts/weekly_ops_summary.py --artifacts artifacts >> "$GITHUB_STEP_SUMMARY"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_TOKEN = re.compile(r"^[a-z0-9_.-]{1,40}$")

# (file, label, dotted path). ``#len`` takes the length of a list.
FIELDS: tuple[tuple[str, str, str], ...] = (
    ("weekly-metrics.json", "first_attempt_success", "report.first_attempt_success"),
    ("weekly-metrics.json", "reuse_rate", "report.reuse_rate"),
    ("weekly-metrics.json", "attempts_to_success", "report.attempts_to_success"),
    ("weekly-metrics.json", "cost_per_solved_task", "report.cost_per_solved_task"),
    ("weekly-metrics.json", "regression_rate", "report.regression_rate"),
    ("weekly-metrics.json", "causal_lift.status", "report.causal_lift.status"),
    ("weekly-metrics.json", "causal_lift.estimate", "report.causal_lift.estimate"),
    ("weekly-metrics.json", "causal_lift.independent_runs", "report.causal_lift.independent_runs"),
    ("weekly-metrics.json", "judge_false_pass_rate", "report.judge_false_pass_rate"),
    ("weekly-metrics.json", "judge_isolation_violations", "report.judge_isolation_violations"),
    ("probes.json", "probe_count", "probes#len"),
    ("probes.json", "precision_at_3", "precision_at_3"),
    ("probes.json", "skill_count", "skill_count"),
    ("canary.json", "canary.trials", "trials"),
    ("canary.json", "canary.false_passes", "false_passes"),
    ("canary.json", "canary.false_pass_rate", "false_pass_rate"),
    ("canary-live.json", "canary_live.trials", "trials"),
    ("canary-live.json", "canary_live.false_passes", "false_passes"),
    ("soak-log.json", "soak.weeks_logged", "weeks#len"),
    ("soak-log.json", "soak.latest_counted", "weeks.-1.counted"),
    ("soak-log.json", "soak.latest_reason", "weeks.-1.reason"),
    ("soak-log.json", "soak.latest_observation_count", "weeks.-1.observation_count"),
    ("soak-log.json", "soak.ga_claimed", "ga_claimed"),
)

_MISSING = object()


def _get(doc: Any, path: str) -> Any:
    head, _, tail = path.partition("#")
    cur = doc
    for part in head.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part, _MISSING)
        elif isinstance(cur, list) and re.fullmatch(r"-?\d+", part):
            idx = int(part)
            cur = cur[idx] if -len(cur) <= idx < len(cur) else _MISSING
        else:
            return _MISSING
        if cur is _MISSING:
            return _MISSING
    if tail == "len":
        return len(cur) if isinstance(cur, list) else _MISSING
    return cur


def render_value(value: Any) -> str:
    if value is _MISSING:
        return "(missing)"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return f"{value:.4g}" if isinstance(value, float) else str(value)
    if isinstance(value, str) and _TOKEN.match(value):
        return value
    return "(omitted)"


def summarise(artifacts: Path) -> str:
    docs: dict[str, Any] = {}
    for name in {f for f, _, _ in FIELDS}:
        path = artifacts / name
        try:
            docs[name] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            docs[name] = _MISSING
    lines = ["## weekly-ops (aggregate counts only)", "", "| metric | value |", "|---|---|"]
    for name, label, path in FIELDS:
        doc = docs[name]
        value = _MISSING if doc is _MISSING else _get(doc, path)
        lines.append(f"| {label} | {render_value(value)} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    args = parser.parse_args(argv)
    sys.stdout.write(summarise(args.artifacts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
