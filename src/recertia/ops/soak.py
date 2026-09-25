"""Soak-week log (RW-GA). Records weeks; never declares operator GA.

An empty eval DB, a golden-only fixture week, or a checkout with no live
``repo-chore`` observations is written down and **not counted**. Four
consecutive counted weeks plus a passing tabletop is ``gate_ready``. This
module never sets ``ga_claimed``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BASELINE_FIELDS = (
    "reuse_rate",
    "first_attempt_success",
    "attempts_to_success",
    "cost_per_solved_task",
)

EMPTY_EVAL = "empty_eval_db"


def iso_week(at: datetime | date | None = None) -> str:
    when = at or datetime.now(timezone.utc)
    if isinstance(when, datetime):
        when = when.date()
    year, week, _day = when.isocalendar()
    return f"{year}-W{week:02d}"


def parse_iso_week(week: str) -> date:
    year_s, sep, week_s = week.partition("-W")
    if sep != "-W":
        raise ValueError(f"week must look like 2026-W33, got {week!r}")
    return date.fromisocalendar(int(year_s), int(week_s), 1)


def next_iso_week(week: str) -> str:
    return iso_week(parse_iso_week(week) + timedelta(days=7))


def _as_report(payload: dict[str, Any]) -> dict[str, Any]:
    report = payload.get("report")
    if isinstance(report, dict):
        return report
    return payload


def observation_count(report: dict[str, Any]) -> int:
    """Live (non-fixture) observations implied by the weekly payload."""

    if report.get("first_attempt_success") is not None:
        lift = report.get("causal_lift") or {}
        treatment = int((lift.get("treatment") or {}).get("trials") or 0)
        control = int((lift.get("control") or {}).get("trials") or 0)
        return max(treatment + control, 1)
    lift = report.get("causal_lift") or {}
    treatment = int((lift.get("treatment") or {}).get("trials") or 0)
    control = int((lift.get("control") or {}).get("trials") or 0)
    return treatment + control


def _baselines(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    unavailable = report.get("unavailable") or {}
    out: dict[str, dict[str, Any]] = {}
    for field in BASELINE_FIELDS:
        out[field] = {
            "value": report.get(field),
            "unavailable": unavailable.get(field),
        }
    return out


@dataclass(frozen=True)
class SoakWeek:
    week: str
    counted: bool
    reason: str | None
    observation_count: int
    claim: str | None
    baselines: dict[str, dict[str, Any]]
    retrieval_precision_at_3: float | None
    canary_attribution: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "week": self.week,
            "counted": self.counted,
            "reason": self.reason,
            "observation_count": self.observation_count,
            "claim": self.claim,
            "baselines": self.baselines,
            "retrieval_precision_at_3": self.retrieval_precision_at_3,
            "canary_attribution": self.canary_attribution,
            "ga_claimed": False,
        }


def classify_week(
    payload: dict[str, Any],
    *,
    week: str | None = None,
    probes: dict[str, Any] | None = None,
    canary: dict[str, Any] | None = None,
) -> SoakWeek:
    """Decide whether this weekly-metrics payload is a soak week."""

    report = _as_report(payload)
    at_raw = report.get("at")
    derived_week = week
    if derived_week is None and isinstance(at_raw, str):
        try:
            derived_week = iso_week(datetime.fromisoformat(at_raw.replace("Z", "+00:00")))
        except ValueError:
            derived_week = None
    derived_week = derived_week or iso_week()
    n = observation_count(report)
    precision = report.get("retrieval_precision_at_3")
    if precision is None and probes is not None:
        precision = probes.get("precision_at_3")
    attribution = None
    canary_payload = canary if canary is not None else payload.get("canary")
    if isinstance(canary_payload, dict):
        attribution = canary_payload.get("attribution") or canary_payload.get("model_version")
    if n <= 0:
        return SoakWeek(
            week=derived_week,
            counted=False,
            reason=EMPTY_EVAL,
            observation_count=0,
            claim=payload.get("claim") or (report.get("causal_lift") or {}).get("status"),
            baselines=_baselines(report),
            retrieval_precision_at_3=precision,
            canary_attribution=attribution,
        )
    return SoakWeek(
        week=derived_week,
        counted=True,
        reason=None,
        observation_count=n,
        claim=payload.get("claim") or (report.get("causal_lift") or {}).get("status"),
        baselines=_baselines(report),
        retrieval_precision_at_3=precision,
        canary_attribution=attribution,
    )


def load_log(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"weeks": [], "ga_claimed": False}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("soak log must be a JSON object")
    payload.setdefault("weeks", [])
    payload["ga_claimed"] = False
    return payload


def record_week(log: dict[str, Any], week: SoakWeek) -> dict[str, Any]:
    weeks = [w for w in log.get("weeks", []) if w.get("week") != week.week]
    weeks.append(week.as_dict())
    weeks.sort(key=lambda w: w["week"])
    return {"weeks": weeks, "ga_claimed": False}


def consecutive_counted(weeks: list[dict[str, Any]]) -> int:
    counted = [w for w in weeks if w.get("counted")]
    if not counted:
        return 0
    counted.sort(key=lambda w: w["week"])
    run = 1
    for prev, cur in zip(counted, counted[1:], strict=False):
        if cur["week"] == next_iso_week(prev["week"]):
            run += 1
        else:
            run = 1
    return run


def tabletop_ok(tabletop: dict[str, Any] | None) -> tuple[bool, str | None]:
    if tabletop is None:
        return False, "tabletop_missing"
    if tabletop.get("ga_claimed"):
        return False, "tabletop_claimed_ga"
    if not tabletop.get("pass"):
        return False, "tabletop_failed"
    return True, None


def status(
    log: dict[str, Any],
    *,
    tabletop: dict[str, Any] | None = None,
    required_weeks: int = 4,
) -> dict[str, Any]:
    weeks = list(log.get("weeks") or [])
    streak = consecutive_counted(weeks)
    top_ok, top_reason = tabletop_ok(tabletop)
    missing: list[str] = []
    if streak < required_weeks:
        missing.append(f"counted_weeks={streak}<{required_weeks}")
    if not top_ok and top_reason:
        missing.append(top_reason)
    return {
        "counted_weeks": streak,
        "required_weeks": required_weeks,
        "weeks_recorded": len(weeks),
        "tabletop_ok": top_ok,
        "tabletop_reason": top_reason,
        "gate_ready": streak >= required_weeks and top_ok,
        "ga_claimed": False,
        "missing": missing,
        "weeks": weeks,
    }


def write_log(path: Path, log: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"weeks": log.get("weeks") or [], "ga_claimed": False}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} is not a JSON object")
    return payload
