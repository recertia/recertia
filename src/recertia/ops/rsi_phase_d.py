"""Phase D soak record. Not a flag flip.

The window closes on the later of 14 days and 30 promotion decisions.
A ranking flip, or a control-arm drop outside the pre-registered noise
band, aborts. Abort restores the index through the Phase C rollback and
forces the opt-in flags off. A completed window does not change a flag
default and does not schedule default-on.
"""

from __future__ import annotations

import json
from collections.abc import MutableMapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from recertia.autonomy import DEFAULT_AUTONOMY
from recertia.evals.metrics import first_attempt_success_sample
from recertia.ledger.hashchain import HashChainLedger
from recertia.ops.rsi_phase_c import DurableIndex, rollback_dry_run
from recertia.rsi.models import ScoreSidecar
from recertia.rsi.pipeline import should_rollback
from recertia.rsi.scoring import soak_must_abort

WINDOW_DAYS = 14
WINDOW_DECISIONS = 30
_ACTOR = "rsi-phase-d"
_BAND_KIND = "rsi_noise_band"
_FLAG_NAMES = (
    "RECERTIA_HYGIENE_ENABLED",
    "RECERTIA_CALIBRATED_SCORING",
    "RECERTIA_PHASE4_RETIREMENT",
)


@dataclass(frozen=True)
class SoakSkill:
    """One skill the soak compares. Retirement still reads ``legacy``."""

    skill_id: str
    legacy: float
    prioritized: float


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("soak clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _aware(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sidecar(skill: SoakSkill) -> ScoreSidecar:
    return ScoreSidecar(
        run_id=skill.skill_id,
        legacy=skill.legacy,
        dense=0.0,
        prioritized=skill.prioritized,
        constants_version="unset",
        strict_success=False,
        task_class="unset",
        checks_passed=0,
        checks_total=0,
    )


def window_state(*, started_at: datetime, now: datetime, decision_count: int) -> dict[str, Any]:
    """Complete only when both gates are met. The unmet one is the later gate."""

    started = _aware(started_at)
    current = _aware(now)
    elapsed = (current - started).total_seconds() / 86400.0
    if elapsed < 0:
        elapsed = 0.0
    days_met = elapsed >= WINDOW_DAYS
    decisions_met = decision_count >= WINDOW_DECISIONS
    if days_met and decisions_met:
        blocking: str | None = None
    elif not days_met and not decisions_met:
        blocking = "both"
    elif not days_met:
        blocking = "days"
    else:
        blocking = "decisions"
    return {
        "elapsed_days": elapsed,
        "decisions": decision_count,
        "days_required": WINDOW_DAYS,
        "decisions_required": WINDOW_DECISIONS,
        "days_met": days_met,
        "decisions_met": decisions_met,
        "complete": days_met and decisions_met,
        "blocking": blocking,
    }


def record_promotion(
    ledger: HashChainLedger,
    *,
    snapshot_id: str,
    at: datetime,
    index_generation: int,
    active_skill_ids: list[str],
) -> None:
    """One promotion decision. Rows before the soak start do not count."""

    ledger.append(
        actor=_ACTOR,
        action="write",
        target=snapshot_id,
        at=_aware(at),
        evidence={
            "kind": "rsi_stage",
            "snapshot_id": snapshot_id,
            "stage": "promote",
            "index_generation": index_generation,
            "active_skill_ids": list(active_skill_ids),
        },
    )


def promotion_decision_times(
    ledger: HashChainLedger, *, started_at: datetime, now: datetime
) -> list[datetime]:
    started = _aware(started_at)
    current = _aware(now)
    found: list[datetime] = []
    for entry in ledger.entries():
        evidence = entry.evidence or {}
        if evidence.get("kind") != "rsi_stage" or evidence.get("stage") != "promote":
            continue
        at = entry.at
        if at.tzinfo is None or at.tzinfo.utcoffset(at) is None:
            at = at.replace(tzinfo=timezone.utc)
        else:
            at = at.astimezone(timezone.utc)
        if started <= at <= current:
            found.append(at)
    found.sort()
    return found


def _baseline_promote(ledger: HashChainLedger, snapshot_id: str) -> dict[str, Any] | None:
    found: dict[str, Any] | None = None
    for entry in ledger.entries():
        evidence = entry.evidence or {}
        if (
            evidence.get("kind") == "rsi_stage"
            and evidence.get("stage") == "promote"
            and evidence.get("snapshot_id") == snapshot_id
            and evidence.get("baseline") is True
        ):
            found = evidence
    return found


def ensure_baseline_promote(
    ledger: HashChainLedger,
    index: DurableIndex,
    *,
    snapshot_id: str,
    at: datetime,
) -> dict[str, Any]:
    """One pre-change promote row. A second call does not append another.

    The timestamp is the caller's. Pass a time before the soak start so this
    restore point is not one of the 30 in-window decisions. The row records
    the live generation at call time. Call it before a later index commit.
    """

    existing = _baseline_promote(ledger, snapshot_id)
    if existing is not None:
        return {
            "written": False,
            "snapshot_id": snapshot_id,
            "baseline": True,
            "index_generation": int(existing["index_generation"]),
            "active_skill_ids": [str(item) for item in existing.get("active_skill_ids") or []],
        }
    generation = index.live_generation()
    skill_ids = index.live_ids()
    ledger.append(
        actor=_ACTOR,
        action="write",
        target=snapshot_id,
        at=_aware(at),
        evidence={
            "kind": "rsi_stage",
            "snapshot_id": snapshot_id,
            "stage": "promote",
            "baseline": True,
            "index_generation": generation,
            "active_skill_ids": list(skill_ids),
        },
    )
    return {
        "written": True,
        "snapshot_id": snapshot_id,
        "baseline": True,
        "index_generation": generation,
        "active_skill_ids": skill_ids,
    }


def register_noise_band(
    ledger: HashChainLedger,
    *,
    soak_id: str,
    noise_band: float,
    at: datetime,
) -> dict[str, Any]:
    """Write the band once, before any control-arm comparison.

    A later call with a different number is refused. The same number is a replay.
    """

    if isinstance(noise_band, bool) or not isinstance(noise_band, (int, float)):
        raise ValueError("noise band must be a number")
    band = float(noise_band)
    if band < 0:
        raise ValueError("noise band must be >= 0")
    prior = registered_noise_band(ledger, soak_id)
    if prior is not None:
        if prior != band:
            raise PermissionError("noise band already registered")
        return {"written": False, "soak_id": soak_id, "noise_band": prior}
    ledger.append(
        actor=_ACTOR,
        action="write",
        target=soak_id,
        at=_aware(at),
        evidence={"kind": _BAND_KIND, "soak_id": soak_id, "noise_band": band},
    )
    return {"written": True, "soak_id": soak_id, "noise_band": band}


def registered_noise_band(ledger: HashChainLedger, soak_id: str) -> float | None:
    found: float | None = None
    for entry in ledger.entries():
        evidence = entry.evidence or {}
        if evidence.get("kind") == _BAND_KIND and evidence.get("soak_id") == soak_id:
            found = float(evidence["noise_band"])
    return found


def control_arm_rate(observations: list[dict[str, Any]]) -> dict[str, Any]:
    """First-attempt success on the existing soak's control arm.

    Rows with any other ``arm`` are ignored. No control rows yields a null rate.
    """

    control = [row for row in observations if row.get("arm") == "control"]
    sample = first_attempt_success_sample(control)
    return {"trials": sample.trials, "successes": sample.successes, "rate": sample.rate}


def skills_from_sidecars(ledger: HashChainLedger) -> list[SoakSkill]:
    """Sidecar rows that name a skill. A row without ``skill_id`` is not a watch."""

    found: list[SoakSkill] = []
    seen: set[str] = set()
    for entry in ledger.entries():
        evidence = entry.evidence or {}
        if evidence.get("kind") != "rsi_score_sidecar":
            continue
        skill_id = evidence.get("skill_id")
        if not skill_id or str(skill_id) in seen:
            continue
        seen.add(str(skill_id))
        found.append(
            SoakSkill(
                skill_id=str(skill_id),
                legacy=float(evidence["legacy"]),
                prioritized=float(evidence["prioritized"]),
            )
        )
    return found


def watch_skills(skills: list[SoakSkill]) -> list[SoakSkill]:
    """One row per skill. An empty list cannot open the soak."""

    if not skills:
        raise ValueError("an empty skill list cannot open a soak")
    ids = [skill.skill_id for skill in skills]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate skill_id")
    return list(skills)


def flipped_skills(skills: list[SoakSkill], threshold: float) -> list[str]:
    """Legacy would keep the skill. Prioritized would drop it. A finding, not a switch."""

    sidecars = [_sidecar(skill) for skill in skills]
    found = [
        skill.skill_id
        for skill in skills
        if skill.legacy >= threshold and skill.prioritized < threshold
    ]
    if bool(found) != soak_must_abort(sidecars, threshold):
        raise RuntimeError("soak abort predicate disagreed with the skill list")
    return found


def force_flags_off(environ: MutableMapping[str, str] | None = None) -> dict[str, Any]:
    """Drop opt-in flags. Never sets one. Code defaults stay off."""

    env = environ if environ is not None else _process_environ()
    removed = [name for name in _FLAG_NAMES if env.pop(name, None) is not None]
    return {
        "removed": removed,
        "hygiene_enabled": False,
        "calibrated_scoring": False,
        "phase4_retirement": False,
    }


def _process_environ() -> MutableMapping[str, str]:
    import os

    return os.environ


def checkout_record(now: datetime) -> dict[str, Any]:
    """This checkout has no live soak window. Not a pass and not an abort."""

    current = _aware(now)
    window = window_state(started_at=current, now=current, decision_count=0)
    return _result(
        status="open",
        started_at=current,
        now=current,
        window=window,
        aborted=False,
        passed=False,
        findings=[],
        open_reasons=["window", "no_retirement_rows", "control_arm_unmeasured"],
        threshold=DEFAULT_AUTONOMY.retirement_threshold,
        retirement_rows=0,
        control=_unmeasured_control(),
        rollback_generation=None,
        flags_forced_off=False,
        note=(
            "No live promotion-decision window is on this checkout. "
            "The abort predicate is implemented and tested. Default-on is not scheduled."
        ),
    )


def evaluate_soak(
    ledger: HashChainLedger,
    index: DurableIndex,
    *,
    started_at: datetime,
    now: datetime,
    skills: list[SoakSkill],
    baseline_snapshot_id: str,
    post_change_skill_id: str,
    threshold: float | None = None,
    baseline_rate: float | None = None,
    current_rate: float | None = None,
    noise_band: float | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> dict[str, Any]:
    """Write the soak result. Abort rolls back. A pass does not turn a flag on."""

    started = _aware(started_at)
    current = _aware(now)
    cutoff = threshold if threshold is not None else DEFAULT_AUTONOMY.retirement_threshold
    decisions = promotion_decision_times(ledger, started_at=started, now=current)
    window = window_state(started_at=started, now=current, decision_count=len(decisions))
    control = _control_arm(baseline_rate, current_rate, noise_band)
    findings = _findings(skills, cutoff, control)
    open_reasons = _open_reasons(window, skills, control)
    aborted = bool(findings)
    passed = not aborted and not open_reasons
    status = "aborted" if aborted else "passed" if passed else "open"
    planned = _result(
        status=status,
        started_at=started,
        now=current,
        window=window,
        aborted=aborted,
        passed=passed,
        findings=findings,
        open_reasons=open_reasons,
        threshold=cutoff,
        retirement_rows=len(skills),
        control=control,
        rollback_generation=None,
        flags_forced_off=aborted,
        note=_note(status),
    )
    soak_id = f"{baseline_snapshot_id}:{_iso(started)}"
    planned["soak_id"] = soak_id
    planned["baseline_snapshot_id"] = baseline_snapshot_id
    latest = _latest_soak(ledger, soak_id)
    if aborted:
        forced = force_flags_off(environ)
        planned["hygiene_enabled"] = forced["hygiene_enabled"]
        planned["calibrated_scoring"] = forced["calibrated_scoring"]
        planned["phase4_retirement"] = forced["phase4_retirement"]
    if latest is not None and _same(latest, planned):
        return _public(latest)
    if aborted:
        rolled = rollback_dry_run(
            ledger,
            index,
            baseline_snapshot_id=baseline_snapshot_id,
            post_change_skill_id=post_change_skill_id,
        )
        planned["rollback_generation"] = rolled["generation"]
        if index.live_generation() != int(rolled["generation"]):
            raise RuntimeError("abort did not restore the index pointer")
    _append_soak(ledger, planned)
    return planned


def open_soak(
    ledger: HashChainLedger,
    index: DurableIndex,
    *,
    started_at: datetime,
    now: datetime,
    baseline_snapshot_id: str,
    post_change_skill_id: str,
    noise_band: float,
    skills: list[SoakSkill] | None = None,
    observations: list[dict[str, Any]] | None = None,
    threshold: float | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> dict[str, Any]:
    """Write the baseline promote, register the band, then watch the skills.

    The promote is stamped one second before ``started_at``, so it is the
    restore point and not a soak decision. The current control-arm rate is
    left unmeasured. Copying the baseline into the current rate would let a
    later window pass without a new measurement.
    """

    started = _aware(started_at)
    current = _aware(now)
    soak_id = f"{baseline_snapshot_id}:{_iso(started)}"
    baseline = ensure_baseline_promote(
        ledger,
        index,
        snapshot_id=baseline_snapshot_id,
        at=started - timedelta(seconds=1),
    )
    band = register_noise_band(ledger, soak_id=soak_id, noise_band=noise_band, at=started)
    rate = control_arm_rate(list(observations or []))
    watched = skills_from_sidecars(ledger) if skills is None else list(skills)
    if not watched:
        return {
            "status": "not_opened",
            "soak_id": soak_id,
            "passed": False,
            "aborted": False,
            "default_on": False,
            "default_on_scheduled": False,
            "flags_default_changed": False,
            "authorization": "none",
            "reason": "no_retirement_rows",
            "baseline_promote": baseline,
            "noise_band": band,
            "control_arm_baseline": rate,
            "skills": [],
            "retirement_threshold": (
                DEFAULT_AUTONOMY.retirement_threshold if threshold is None else threshold
            ),
        }
    watched = watch_skills(watched)
    result = evaluate_soak(
        ledger,
        index,
        started_at=started,
        now=current,
        skills=watched,
        baseline_snapshot_id=baseline_snapshot_id,
        post_change_skill_id=post_change_skill_id,
        threshold=threshold,
        baseline_rate=rate["rate"],
        current_rate=None,
        noise_band=band["noise_band"],
        environ=environ,
    )
    result["baseline_promote"] = baseline
    result["noise_band_registration"] = band
    result["control_arm_baseline"] = rate
    return result


def _control_arm(
    baseline_rate: float | None,
    current_rate: float | None,
    noise_band: float | None,
) -> dict[str, Any]:
    if noise_band is not None and noise_band < 0:
        raise ValueError("noise band must be >= 0")
    if current_rate is not None and baseline_rate is None:
        raise ValueError("current rate requires a baseline rate")
    if baseline_rate is None or current_rate is None or noise_band is None:
        return {
            "measured": False,
            "baseline": baseline_rate,
            "current": current_rate,
            "noise_band": noise_band,
            "regressed": False,
        }
    regressed = should_rollback(current_rate, baseline_rate, noise_band)
    return {
        "measured": True,
        "baseline": baseline_rate,
        "current": current_rate,
        "noise_band": noise_band,
        "regressed": regressed,
    }


def _unmeasured_control() -> dict[str, Any]:
    return {
        "measured": False,
        "baseline": None,
        "current": None,
        "noise_band": None,
        "regressed": False,
    }


def _findings(
    skills: list[SoakSkill], threshold: float, control: dict[str, Any]
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    flipped = flipped_skills(skills, threshold)
    if flipped:
        findings.append(
            {
                "kind": "ranking_flip",
                "skill_ids": flipped,
                "detail": (
                    "legacy score would keep a skill the prioritized score would drop"
                ),
            }
        )
    if control["regressed"]:
        findings.append(
            {
                "kind": "control_arm",
                "skill_ids": [],
                "baseline": control["baseline"],
                "current": control["current"],
                "noise_band": control["noise_band"],
                "detail": (
                    "control-arm first-attempt success fell outside the pre-registered noise band"
                ),
            }
        )
    return findings


def _open_reasons(
    window: dict[str, Any], skills: list[SoakSkill], control: dict[str, Any]
) -> list[str]:
    reasons: list[str] = []
    if not window["complete"]:
        reasons.append("window")
    if not skills:
        reasons.append("no_retirement_rows")
    if not control["measured"]:
        reasons.append("control_arm_unmeasured")
    return reasons


def _note(status: str) -> str:
    if status == "aborted":
        return "Finding only. Not an authorization to switch scores or to turn a flag on."
    if status == "passed":
        return "Window met. Default-on is a separate decision and is not scheduled."
    return "Soak is open. Flags were not changed."


def _result(
    *,
    status: str,
    started_at: datetime,
    now: datetime,
    window: dict[str, Any],
    aborted: bool,
    passed: bool,
    findings: list[dict[str, Any]],
    open_reasons: list[str],
    threshold: float,
    retirement_rows: int,
    control: dict[str, Any],
    rollback_generation: int | None,
    flags_forced_off: bool,
    note: str,
) -> dict[str, Any]:
    return {
        "kind": "rsi_soak",
        "status": status,
        "started_at": _iso(started_at),
        "evaluated_at": _iso(now),
        "elapsed_days": window["elapsed_days"],
        "decisions": window["decisions"],
        "days_required": WINDOW_DAYS,
        "decisions_required": WINDOW_DECISIONS,
        "window_complete": window["complete"],
        "blocking": window["blocking"],
        "aborted": aborted,
        "passed": passed,
        "findings": findings,
        "open_reasons": open_reasons,
        "authorization": "none",
        "default_on": False,
        "default_on_scheduled": False,
        "flags_default_changed": False,
        "flags_forced_off": flags_forced_off,
        "retirement_threshold": threshold,
        "retirement_rows": retirement_rows,
        "control_arm": control,
        "rollback_generation": rollback_generation,
        "note": note,
    }


def _latest_soak(ledger: HashChainLedger, soak_id: str) -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None
    for entry in ledger.entries():
        evidence = entry.evidence or {}
        if evidence.get("kind") == "rsi_soak" and evidence.get("soak_id") == soak_id:
            latest = evidence
    return latest


def _same(previous: dict[str, Any], planned: dict[str, Any]) -> bool:
    keys = (
        "status",
        "started_at",
        "evaluated_at",
        "decisions",
        "window_complete",
        "aborted",
        "passed",
        "findings",
        "open_reasons",
        "authorization",
        "default_on",
        "default_on_scheduled",
        "flags_default_changed",
    )
    return all(previous.get(key) == planned.get(key) for key in keys)


def _public(evidence: dict[str, Any]) -> dict[str, Any]:
    return dict(evidence)


def _append_soak(ledger: HashChainLedger, result: dict[str, Any]) -> str:
    entry = ledger.append(
        actor=_ACTOR,
        action="write",
        target=str(result["soak_id"]),
        evidence=json.loads(json.dumps(result)),
    )
    return str(entry.entry_hash)
