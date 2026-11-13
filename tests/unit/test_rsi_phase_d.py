"""Phase D. Soak record, abort, rollback. No flag default change."""

from __future__ import annotations

import ast
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from recertia.ledger.hashchain import HashChainLedger
from recertia.ops.rsi_phase_c import DurableIndex
from recertia.ops.rsi_phase_d import (
    NoiseBandMismatchError,
    SoakSkill,
    checkout_record,
    control_arm_rate,
    evaluate_soak,
    flipped_skills,
    open_soak,
    record_promotion,
    register_noise_band,
    window_state,
)
from recertia.rsi.world import Flags

_ROOT = Path(__file__).resolve().parents[2]
_PHASE = _ROOT / "src" / "recertia" / "ops" / "rsi_phase_d.py"
_SOAK_FILE = _ROOT / "docs" / "ops" / "rsi-soak.json"
_START = datetime(2026, 11, 1, tzinfo=timezone.utc)
_SOAK_ID = "base:2026-11-01T00:00:00Z"


def _ledger(tmp_path: Path) -> HashChainLedger:
    return HashChainLedger(tmp_path / "ledger.jsonl")


def _index(ledger: HashChainLedger) -> DurableIndex:
    index = DurableIndex(ledger)
    index.prepare("base", ["sk_old"], "man_base")
    index.commit_generation(snapshot_id="base", manifest_id="man_base", passed=True)
    return index


def _later(index: DurableIndex) -> None:
    index.prepare("later", ["sk_old", "sk_new"], "man_later")
    index.commit_generation(snapshot_id="later", manifest_id="man_later", passed=True)


def _decisions(ledger: HashChainLedger, count: int, *, start: datetime = _START) -> None:
    for offset in range(count):
        record_promotion(
            ledger,
            snapshot_id="base",
            at=start + timedelta(hours=offset),
            index_generation=1,
            active_skill_ids=["sk_old"],
        )


def _band(ledger: HashChainLedger, band: float = 0.05) -> None:
    register_noise_band(ledger, soak_id=_SOAK_ID, noise_band=band, at=_START)


def _kept() -> SoakSkill:
    return SoakSkill(skill_id="sk_kept", legacy=10.0, prioritized=10.0)


def _flip() -> SoakSkill:
    return SoakSkill(skill_id="sk_flip", legacy=10.0, prioritized=0.01)


def test_window_waits_for_the_later_gate() -> None:
    early = window_state(
        started_at=_START, now=_START + timedelta(days=14, seconds=-1), decision_count=30
    )
    assert early["complete"] is False
    assert early["blocking"] == "days"
    short = window_state(
        started_at=_START, now=_START + timedelta(days=14), decision_count=29
    )
    assert short["complete"] is False
    assert short["blocking"] == "decisions"
    both = window_state(started_at=_START, now=_START + timedelta(days=13), decision_count=10)
    assert both["blocking"] == "both"
    met = window_state(started_at=_START, now=_START + timedelta(days=14), decision_count=30)
    assert met["complete"] is True
    assert met["blocking"] is None
    assert met["days_required"] == 14
    assert met["decisions_required"] == 30


def test_decisions_outside_the_window_do_not_count(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    index = _index(ledger)
    _decisions(ledger, 30, start=_START - timedelta(days=40))
    _band(ledger)
    result = evaluate_soak(
        ledger,
        index,
        started_at=_START,
        now=_START + timedelta(days=20),
        skills=[_kept()],
        baseline_snapshot_id="base",
        post_change_skill_id="sk_new",
        baseline_rate=0.5,
        current_rate=0.5,
        noise_band=0.05,
    )
    assert result["decisions"] == 0
    assert result["status"] == "open"
    assert result["passed"] is False
    assert "window" in result["open_reasons"]
    assert index.live_generation() == 1


def test_ranking_flip_aborts_and_restores_without_authorizing_a_switch(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    index = _index(ledger)
    _later(index)
    record_promotion(
        ledger,
        snapshot_id="base",
        at=_START + timedelta(days=1),
        index_generation=1,
        active_skill_ids=["sk_old"],
    )
    env = {
        "RECERTIA_HYGIENE_ENABLED": "1",
        "RECERTIA_CALIBRATED_SCORING": "1",
        "RECERTIA_PHASE4_RETIREMENT": "1",
        "OTHER": "keep",
    }
    result = evaluate_soak(
        ledger,
        index,
        started_at=_START,
        now=_START + timedelta(days=2),
        skills=[_flip(), _kept()],
        baseline_snapshot_id="base",
        post_change_skill_id="sk_new",
        threshold=0.05,
        environ=env,
    )
    assert result["status"] == "aborted"
    assert result["passed"] is False
    assert result["authorization"] == "none"
    assert result["default_on"] is False
    assert result["default_on_scheduled"] is False
    assert result["flags_default_changed"] is False
    assert result["findings"][0]["kind"] == "ranking_flip"
    assert result["findings"][0]["skill_ids"] == ["sk_flip"]
    assert result["rollback_generation"] == 1
    assert result["hygiene_enabled"] is False
    assert result["calibrated_scoring"] is False
    assert index.live_generation() == 1
    assert index.retrieve("sk_new") == []
    assert index.retrieve("sk_old") == ["sk_old"]
    assert "RECERTIA_HYGIENE_ENABLED" not in env
    assert "RECERTIA_CALIBRATED_SCORING" not in env
    assert "RECERTIA_PHASE4_RETIREMENT" not in env
    assert env["OTHER"] == "keep"
    kinds = [entry.evidence.get("kind") for entry in ledger.entries()]
    assert kinds.count("rsi_incident") == 1
    assert kinds.count("rsi_soak") == 1
    replay_env = {"RECERTIA_HYGIENE_ENABLED": "1"}
    again = evaluate_soak(
        ledger,
        index,
        started_at=_START,
        now=_START + timedelta(days=2),
        skills=[_flip(), _kept()],
        baseline_snapshot_id="base",
        post_change_skill_id="sk_new",
        threshold=0.05,
        environ=replay_env,
    )
    assert again["status"] == "aborted"
    assert replay_env == {}
    kinds = [entry.evidence.get("kind") for entry in ledger.entries()]
    assert kinds.count("rsi_incident") == 1
    assert kinds.count("rsi_soak") == 1


def test_replay_clears_a_flag_that_was_set_again(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    index = _index(ledger)
    _later(index)
    record_promotion(
        ledger,
        snapshot_id="base",
        at=_START + timedelta(days=1),
        index_generation=1,
        active_skill_ids=["sk_old"],
    )
    evaluate_soak(
        ledger,
        index,
        started_at=_START,
        now=_START + timedelta(days=2),
        skills=[_flip()],
        baseline_snapshot_id="base",
        post_change_skill_id="sk_new",
        environ={},
    )
    again_env = {"RECERTIA_HYGIENE_ENABLED": "1"}
    evaluate_soak(
        ledger,
        index,
        started_at=_START,
        now=_START + timedelta(days=2),
        skills=[_flip()],
        baseline_snapshot_id="base",
        post_change_skill_id="sk_new",
        environ=again_env,
    )
    assert again_env == {}


def test_control_arm_drop_aborts_inside_the_band_does_not(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    index = _index(ledger)
    _later(index)
    record_promotion(
        ledger,
        snapshot_id="base",
        at=_START,
        index_generation=1,
        active_skill_ids=["sk_old"],
    )
    _band(ledger)
    dropped = evaluate_soak(
        ledger,
        index,
        started_at=_START,
        now=_START + timedelta(days=1),
        skills=[_kept()],
        baseline_snapshot_id="base",
        post_change_skill_id="sk_new",
        baseline_rate=0.50,
        current_rate=0.40,
        noise_band=0.05,
        environ={},
    )
    assert dropped["status"] == "aborted"
    assert dropped["findings"][0]["kind"] == "control_arm"
    assert dropped["authorization"] == "none"
    assert index.retrieve("sk_new") == []
    held = _ledger(tmp_path / "held")
    held_index = _index(held)
    _later(held_index)
    record_promotion(
        held,
        snapshot_id="base",
        at=_START,
        index_generation=1,
        active_skill_ids=["sk_old"],
    )
    _band(held)
    edge = evaluate_soak(
        held,
        held_index,
        started_at=_START,
        now=_START + timedelta(days=1),
        skills=[_kept()],
        baseline_snapshot_id="base",
        post_change_skill_id="sk_new",
        baseline_rate=0.50,
        current_rate=0.45,
        noise_band=0.05,
        environ={"RECERTIA_HYGIENE_ENABLED": "1"},
    )
    assert edge["status"] == "open"
    assert edge["aborted"] is False
    assert held_index.live_generation() == 2
    assert edge["flags_forced_off"] is False


def _evaluate_with_band(
    ledger: HashChainLedger, index: DurableIndex, noise_band: float | None
) -> dict[str, object]:
    return evaluate_soak(
        ledger,
        index,
        started_at=_START,
        now=_START + timedelta(days=1),
        skills=[_kept()],
        baseline_snapshot_id="base",
        post_change_skill_id="sk_new",
        baseline_rate=0.50,
        current_rate=0.50,
        noise_band=noise_band,
        environ={},
    )


def test_registered_noise_band_is_accepted(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    index = _index(ledger)
    _band(ledger)
    result = _evaluate_with_band(ledger, index, 0.05)
    assert result["status"] == "open"
    assert result["control_arm"] == {
        "measured": True,
        "baseline": 0.50,
        "current": 0.50,
        "noise_band": 0.05,
        "regressed": False,
    }


@pytest.mark.parametrize("given", [0.10, 0.01, None], ids=["widened", "narrowed", "dropped"])
def test_a_band_other_than_the_registered_one_is_refused(
    tmp_path: Path, given: float | None
) -> None:
    ledger = _ledger(tmp_path)
    index = _index(ledger)
    _band(ledger)
    before = len(list(ledger.entries()))
    with pytest.raises(NoiseBandMismatchError, match="registered band"):
        _evaluate_with_band(ledger, index, given)
    assert len(list(ledger.entries())) == before
    assert index.live_generation() == 1


def test_an_unregistered_band_is_refused(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    index = _index(ledger)
    register_noise_band(ledger, soak_id="other:2026-11-01T00:00:00Z", noise_band=0.05, at=_START)
    with pytest.raises(NoiseBandMismatchError, match="pre-registered"):
        _evaluate_with_band(ledger, index, 0.05)
    kinds = [entry.evidence.get("kind") for entry in ledger.entries()]
    assert kinds.count("rsi_soak") == 0


def test_both_control_rates_without_a_band_are_refused(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    index = _index(ledger)
    with pytest.raises(ValueError, match="noise band must be pre-registered"):
        _evaluate_with_band(ledger, index, None)
    kinds = [entry.evidence.get("kind") for entry in ledger.entries()]
    assert kinds.count("rsi_soak") == 0


def test_below_threshold_is_not_a_flip() -> None:
    below = SoakSkill(skill_id="sk_low", legacy=0.04, prioritized=0.0)
    assert flipped_skills([below], 0.05) == []
    assert flipped_skills([_flip()], 0.05) == ["sk_flip"]
    boundary = SoakSkill(skill_id="sk_edge", legacy=0.05, prioritized=0.04)
    assert flipped_skills([boundary], 0.05) == ["sk_edge"]


def test_complete_window_does_not_schedule_default_on(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    index = _index(ledger)
    _decisions(ledger, 30)
    _band(ledger)
    env = {"RECERTIA_HYGIENE_ENABLED": "1"}
    result = evaluate_soak(
        ledger,
        index,
        started_at=_START,
        now=_START + timedelta(days=14),
        skills=[_kept()],
        baseline_snapshot_id="base",
        post_change_skill_id="sk_new",
        baseline_rate=0.50,
        current_rate=0.52,
        noise_band=0.05,
        environ=env,
    )
    assert result["status"] == "passed"
    assert result["passed"] is True
    assert result["aborted"] is False
    assert result["window_complete"] is True
    assert result["decisions"] == 30
    assert result["default_on"] is False
    assert result["default_on_scheduled"] is False
    assert result["flags_default_changed"] is False
    assert result["flags_forced_off"] is False
    assert result["authorization"] == "none"
    assert env["RECERTIA_HYGIENE_ENABLED"] == "1"
    assert index.live_generation() == 1
    assert [entry.evidence.get("kind") for entry in ledger.entries()].count("rsi_incident") == 0


def test_unmeasured_control_arm_cannot_pass(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    index = _index(ledger)
    _decisions(ledger, 30)
    result = evaluate_soak(
        ledger,
        index,
        started_at=_START,
        now=_START + timedelta(days=14),
        skills=[_kept()],
        baseline_snapshot_id="base",
        post_change_skill_id="sk_new",
    )
    assert result["status"] == "open"
    assert result["passed"] is False
    assert result["open_reasons"] == ["control_arm_unmeasured"]
    assert index.live_generation() == 1


def test_missing_baseline_still_forces_flags_off(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    index = DurableIndex(ledger)
    env = {"RECERTIA_CALIBRATED_SCORING": "1"}
    try:
        evaluate_soak(
            ledger,
            index,
            started_at=_START,
            now=_START + timedelta(days=1),
            skills=[_flip()],
            baseline_snapshot_id="base",
            post_change_skill_id="sk_new",
            environ=env,
        )
    except KeyError as exc:
        assert "baseline" in str(exc)
    else:
        raise AssertionError("rollback without a promote entry must fail")
    assert env == {}
    assert [entry.evidence.get("kind") for entry in ledger.entries()].count("rsi_soak") == 0


def test_checkout_record_is_not_a_pass() -> None:
    record = checkout_record(datetime(2026, 11, 9, tzinfo=timezone.utc))
    assert record["passed"] is False
    assert record["aborted"] is False
    assert record["default_on"] is False
    assert record["default_on_scheduled"] is False
    assert record["flags_default_changed"] is False
    assert record["days_required"] == 14
    assert record["decisions_required"] == 30
    assert record["authorization"] == "none"
    written = json.loads(_SOAK_FILE.read_text(encoding="utf-8"))
    assert written["passed"] is False
    assert written["aborted"] is False
    assert written["default_on"] is False
    assert written["default_on_scheduled"] is False
    assert written["flags_default_changed"] is False
    assert "Default-on is not scheduled" in written["note"]


def test_phase_d_does_not_change_flag_defaults() -> None:
    text = _PHASE.read_text(encoding="utf-8")
    for name in (
        "RECERTIA_HYGIENE_ENABLED",
        "RECERTIA_CALIBRATED_SCORING",
        "RECERTIA_PHASE4_RETIREMENT",
    ):
        assert f'{name}"] = "1"' not in text
        assert f"{name}'] = '1'" not in text
    assert "phase4=True" not in text
    tree = ast.parse(text)
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    assert "recertia.solver" not in modules
    assert not any(module.startswith("recertia.solver.") for module in modules)
    fresh = Flags()
    assert fresh.hygiene_enabled is False
    assert fresh.calibrated_scoring is False
    assert fresh.phase4_retirement is False
    names = (
        "RECERTIA_HYGIENE_ENABLED",
        "RECERTIA_CALIBRATED_SCORING",
        "RECERTIA_PHASE4_RETIREMENT",
    )
    saved = {name: os.environ.pop(name, None) for name in names}
    try:
        assert Flags.from_env().hygiene_enabled is False
        assert Flags.from_env().calibrated_scoring is False
        assert Flags.from_env().phase4_retirement is False
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value


def test_open_soak_writes_the_baseline_and_locks_the_band(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    index = _index(ledger)
    observations = [
        {"arm": "control", "first_attempt_success": True},
        {"arm": "control", "first_attempt_success": False},
        {"arm": "treatment", "first_attempt_success": True},
    ]
    env = {"RECERTIA_HYGIENE_ENABLED": "1", "OTHER": "keep"}
    opened = open_soak(
        ledger,
        index,
        started_at=_START,
        now=_START,
        baseline_snapshot_id="base",
        post_change_skill_id="sk_new",
        noise_band=0.05,
        skills=[_kept()],
        observations=observations,
        environ=env,
    )
    assert opened["status"] == "open"
    assert opened["passed"] is False
    assert opened["decisions"] == 0
    assert opened["baseline_promote"]["written"] is True
    assert opened["baseline_promote"]["baseline"] is True
    assert opened["baseline_promote"]["index_generation"] == 1
    assert opened["baseline_promote"]["active_skill_ids"] == ["sk_old"]
    assert opened["noise_band_registration"]["written"] is True
    assert opened["noise_band_registration"]["noise_band"] == 0.05
    assert opened["control_arm_baseline"]["trials"] == 2
    assert opened["control_arm_baseline"]["rate"] == 0.5
    assert opened["control_arm"]["measured"] is False
    assert opened["control_arm"]["current"] is None
    assert opened["default_on"] is False
    assert env["RECERTIA_HYGIENE_ENABLED"] == "1"
    assert env["OTHER"] == "keep"
    kinds = [entry.evidence.get("kind") for entry in ledger.entries()]
    assert kinds.count("rsi_stage") == 1
    assert kinds.count("rsi_noise_band") == 1
    again = open_soak(
        ledger,
        index,
        started_at=_START,
        now=_START,
        baseline_snapshot_id="base",
        post_change_skill_id="sk_new",
        noise_band=0.05,
        skills=[_kept()],
        observations=observations,
        environ=env,
    )
    assert again["baseline_promote"]["written"] is False
    assert [entry.evidence.get("kind") for entry in ledger.entries()].count("rsi_noise_band") == 1
    try:
        open_soak(
            ledger,
            index,
            started_at=_START,
            now=_START,
            baseline_snapshot_id="base",
            post_change_skill_id="sk_new",
            noise_band=0.2,
            skills=[_kept()],
            environ=env,
        )
    except PermissionError as exc:
        assert "already registered" in str(exc)
    else:
        raise AssertionError("a second noise band must be refused")


def test_open_soak_without_skills_does_not_start(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    index = _index(ledger)
    opened = open_soak(
        ledger,
        index,
        started_at=_START,
        now=_START,
        baseline_snapshot_id="base",
        post_change_skill_id="sk_new",
        noise_band=0.05,
        skills=[],
    )
    assert opened["status"] == "not_opened"
    assert opened["reason"] == "no_retirement_rows"
    assert opened["passed"] is False
    assert [entry.evidence.get("kind") for entry in ledger.entries()].count("rsi_soak") == 0
    assert control_arm_rate([{"arm": "treatment", "first_attempt_success": True}])["rate"] is None


def test_open_soak_reads_skill_sidecars_and_keeps_the_baseline_on_abort(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    index = _index(ledger)
    ledger.append(
        actor="rsi-phase-c",
        action="write",
        target="sk_kept",
        evidence={
            "kind": "rsi_score_sidecar",
            "skill_id": "sk_kept",
            "run_id": "run-kept",
            "legacy": 10.0,
            "prioritized": 10.0,
        },
    )
    opened = open_soak(
        ledger,
        index,
        started_at=_START,
        now=_START,
        baseline_snapshot_id="base",
        post_change_skill_id="sk_new",
        noise_band=0.05,
    )
    assert opened["status"] == "open"
    assert opened["retirement_rows"] == 1
    _later(index)
    record_promotion(
        ledger,
        snapshot_id="base",
        at=_START + timedelta(days=1),
        index_generation=2,
        active_skill_ids=["sk_old", "sk_new"],
    )
    aborted = evaluate_soak(
        ledger,
        index,
        started_at=_START,
        now=_START + timedelta(days=2),
        skills=[_flip()],
        baseline_snapshot_id="base",
        post_change_skill_id="sk_new",
        noise_band=0.05,
        environ={},
    )
    assert aborted["status"] == "aborted"
    assert aborted["rollback_generation"] == 1
    assert index.retrieve("sk_new") == []
    assert index.retrieve("sk_old") == ["sk_old"]
