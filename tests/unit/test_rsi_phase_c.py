"""Phase C. Golden calibration, sidecar, console, index, lineage, rollback.

Flags stay off. A green calibration script does not install constants.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from contracts.skill import SkillVersion as ContractSkill
from recertia.ledger.hashchain import HashChainLedger
from recertia.memory.episodic import CaseRecord, EpisodicStore
from recertia.ops.rsi_phase_c import (
    DurableIndex,
    append_stage,
    console_snapshot,
    gamma_turn_from_record,
    golden_report,
    kept_by_retirement,
    production_recalibrate,
    record_turn_credit,
    refuse_unapproved_constants,
    retirement_mass,
    rollback_dry_run,
    write_lineage,
    write_score_sidecar,
)
from recertia.rsi.models import CalibConstants, ScoreSidecar, TurnView
from recertia.rsi.scoring import accept_constants, score_run

_ROOT = Path(__file__).resolve().parents[2]
_PHASE = _ROOT / "src" / "recertia" / "ops" / "rsi_phase_c.py"
_GOLDEN_SCRIPT = _ROOT / "scripts" / "calibrate_rsi_golden.py"
_SYNTHETIC = _ROOT / "scripts" / "calibrate_rsi.py"


def _constants() -> CalibConstants:
    return CalibConstants(
        version="calib_phasec",
        family_quantile={"wide": 40.0, "narrow": 10.0},
        approved_by="",
        approved_at="",
    )


def _ledger(tmp_path: Path) -> HashChainLedger:
    return HashChainLedger(tmp_path / "ledger.jsonl")


def test_golden_report_is_not_activated_and_does_not_install() -> None:
    report = golden_report(_ROOT)
    assert report["activated"] is False
    assert report["not_for_activation"] is True
    assert report["corpus"] == "golden"
    assert report["records"] == 10
    assert report["constants"]["approved_by"] == ""
    assert report["constants"]["version"].startswith("calib_golden")
    assert "wide" not in report["constants"]["family_quantile"]
    text = _GOLDEN_SCRIPT.read_text(encoding="utf-8")
    assert "Not for activation" in text
    tree = ast.parse(text)
    called = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "approve_gate" not in called
    assert "put_constants" not in called
    synthetic = _SYNTHETIC.read_text(encoding="utf-8")
    assert "Not for activation" in synthetic


def test_failure_share_gate_needs_fifteen_points() -> None:
    constants = _constants()
    corpus = [score_run(f"f{i}", "wide", 40, 40, False, constants) for i in range(20)]
    corpus += [score_run(f"s{i}", "narrow", 10, 10, True, constants) for i in range(10)]
    ok, detail = accept_constants(corpus)
    assert ok is True
    assert detail["drop"] >= 0.15
    assert detail["spearman_strict"] == 1.0
    golden = golden_report(_ROOT)
    assert golden["detail"]["spearman_strict"] == 1.0
    assert golden["accepted"] is False


def test_sidecar_roundtrip_and_retirement_ignores_prioritized(tmp_path: Path) -> None:
    kept = ScoreSidecar(
        run_id="run-kept",
        legacy=40.0,
        dense=1.0,
        prioritized=0.2,
        constants_version="calib_phasec",
        strict_success=False,
        task_class="wide",
        checks_passed=40,
        checks_total=40,
    )
    dropped = ScoreSidecar(
        run_id="run-dropped",
        legacy=10.0,
        dense=1.0,
        prioritized=9.0,
        constants_version="calib_phasec",
        strict_success=True,
        task_class="narrow",
        checks_passed=10,
        checks_total=10,
    )
    ledger = _ledger(tmp_path)
    first = write_score_sidecar(ledger, kept)
    assert write_score_sidecar(ledger, kept) == first
    kinds = [entry.evidence.get("kind") for entry in ledger.entries()]
    assert kinds.count("rsi_score_sidecar") == 1
    assert retirement_mass(kept) == 40.0
    flipped = ScoreSidecar(
        run_id=kept.run_id,
        legacy=kept.legacy,
        dense=kept.dense,
        prioritized=100.0,
        constants_version=kept.constants_version,
        strict_success=kept.strict_success,
        task_class=kept.task_class,
        checks_passed=kept.checks_passed,
        checks_total=kept.checks_total,
    )
    assert kept_by_retirement([kept, dropped], 20) == ["run-kept"]
    assert kept_by_retirement([flipped, dropped], 20) == ["run-kept"]


def test_console_shows_both_numbers_and_labels_a_liftless_move() -> None:
    constants = _constants()
    low = score_run("a", "narrow", 1, 1, False, constants)
    high = score_run("b", "narrow", 10, 10, True, constants)
    before = console_snapshot([low], 0.4)
    after = console_snapshot([high], 0.4, previous=before)
    assert before["process_score"] != after["process_score"]
    assert after["control_arm_lift"] == 0.4
    assert after["strict_completion"] == 1
    assert "failure_share" in after
    assert after["label"] == "not an improvement"
    assert console_snapshot([high], None)["control_arm_lift"] is None
    assert console_snapshot([high], 0.4, previous=before)["label"] == "not an improvement"
    moved = console_snapshot([high], 0.9, previous=before)
    assert moved["label"] is None


def test_missing_t2_record_uses_unset_gamma(tmp_path: Path) -> None:
    assert gamma_turn_from_record(None) == (0.95, "unset")
    assert gamma_turn_from_record({"gamma_turn": 0.5}) == (0.95, "unset")
    assert gamma_turn_from_record(
        {"approved_by": "operator", "gamma_turn": 0.5, "constants_version": "calib_phasec"}
    ) == (0.5, "calib_phasec")
    with pytest.raises(PermissionError):
        refuse_unapproved_constants({"approved_by": "", "gamma_turn": 0.5})
    ledger = _ledger(tmp_path)
    rows = record_turn_credit(
        ledger,
        artifact_id="scr_abc",
        turns=[TurnView(turn_id="t0", decision_steps=1)],
        prioritized=1.0,
        policy_record=None,
    )
    assert rows[0].constants_version == "unset"
    assert rows[0].gamma_turn == 0.95
    evidence = ledger.entries()[-1].evidence
    assert evidence["kind"] == "rsi_step_credit"
    assert evidence["constants_version"] == "unset"


def test_duration_breach_does_not_move_the_pointer(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    index = DurableIndex(ledger)
    manifest = production_recalibrate(
        golden_root=_ROOT / "evals" / "golden",
        cases=[],
        practice_job_s=100.0,
        duration_s=5.0,
        index=index,
        snapshot_id="snap-breach",
    )
    assert manifest.passed is False
    assert manifest.duration_breach is True
    assert index.live_generation() == 0
    assert index.prepared() is None
    text = _PHASE.read_text(encoding="utf-8")
    assert "run_golden" not in text
    assert "recertia.solver" not in text
    assert "run_in_container" not in text


def test_bad_sample_blocks_and_a_clean_pass_commits(tmp_path: Path) -> None:
    blocked = _ledger(tmp_path / "blocked")
    blocked_index = DurableIndex(blocked)
    store = EpisodicStore(tmp_path / "episodic")
    store.write(
        CaseRecord(
            case_id="case-a",
            run_id="run-a",
            attempt_no=0,
            task_class="repo-chore",
            outcome="solved",
        )
    )
    store.write(
        CaseRecord(
            case_id="case-b",
            run_id="run-b",
            attempt_no=0,
            task_class="repo-chore",
            outcome="failed",
        )
    )
    cases = [
        {"case_id": "case-a", "task_class": "repo-chore", "outcome": "solved"},
        {"case_id": "case-c", "task_class": "other", "outcome": "not-an-outcome"},
    ]
    manifest = production_recalibrate(
        golden_root=_ROOT / "evals" / "golden",
        cases=cases,
        practice_job_s=100.0,
        duration_s=1.0,
        index=blocked_index,
        snapshot_id="snap-sample",
    )
    assert manifest.passed is False
    assert blocked_index.live_generation() == 0
    assert store.list_index()
    clean = DurableIndex(_ledger(tmp_path / "clean"))
    passed = production_recalibrate(
        golden_root=_ROOT / "evals" / "golden",
        cases=[{"case_id": "case-a", "task_class": "repo-chore", "outcome": "solved"}],
        practice_job_s=100.0,
        duration_s=1.0,
        index=clean,
        snapshot_id="snap-clean",
    )
    assert passed.passed is True
    assert clean.live_generation() == 1
    assert "add-editorconfig" in clean.live_ids()


def test_crash_between_prepare_and_commit_keeps_the_prior_generation(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    index = DurableIndex(ledger)
    index.prepare("snap", ["sk_old"], "man_1")
    restarted = DurableIndex(ledger)
    assert restarted.live_generation() == 0
    assert restarted.live_ids() == []
    assert restarted.prepared() is not None
    assert restarted.resume(passed=False) == "aborted"
    assert restarted.live_generation() == 0
    assert restarted.prepared() is None
    index.prepare("snap", ["sk_old"], "man_2")
    resumed = DurableIndex(ledger)
    assert resumed.resume(passed=True, snapshot_id="snap", manifest_id="man_2") == "committed"
    assert resumed.live_generation() == 1
    assert resumed.retrieve("sk_old") == ["sk_old"]
    assert resumed.retrieve("sk_new") == []


def test_lineage_is_a_ledger_row_and_golden_skills_still_validate(tmp_path: Path) -> None:
    version_path = _ROOT / "skills" / "add-editorconfig" / "v1" / "version.json"
    before = version_path.read_text(encoding="utf-8")
    loaded = ContractSkill.model_validate(json.loads(before))
    assert loaded.skill_id == "add-editorconfig"
    assert "screened_artifact_id" not in ContractSkill.model_fields
    ledger = _ledger(tmp_path)
    write_lineage(
        ledger,
        skill_id=loaded.skill_id,
        screened_artifact_id="scr_0123456789abcdef",
        payload_sha256="a" * 64,
        criteria_hash="b" * 64,
        recalibration_manifest_id="man_0123456789abcdef",
    )
    evidence = ledger.entries()[-1].evidence
    assert evidence["kind"] == "rsi_lineage"
    assert evidence["screened_artifact_id"].startswith("scr_")
    assert version_path.read_text(encoding="utf-8") == before


def test_rollback_dry_run_on_the_production_ledger(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    index = DurableIndex(ledger)
    index.prepare("base", ["sk_old"], "man_base")
    index.commit_generation(snapshot_id="base", manifest_id="man_base", passed=True)
    append_stage(
        ledger,
        snapshot_id="base",
        stage="promote",
        index_generation=1,
        active_skill_ids=["sk_old"],
    )
    index.prepare("later", ["sk_old", "sk_new"], "man_later")
    index.commit_generation(snapshot_id="later", manifest_id="man_later", passed=True)
    assert index.retrieve("sk_new") == ["sk_new"]
    result = rollback_dry_run(
        ledger,
        index,
        baseline_snapshot_id="base",
        post_change_skill_id="sk_new",
    )
    assert result["generation"] == 1
    assert result["hygiene_enabled"] is False
    assert result["calibrated_scoring"] is False
    assert index.retrieve("sk_new") == []
    assert index.retrieve("sk_old") == ["sk_old"]
    kinds = [entry.evidence.get("kind") for entry in ledger.entries()]
    assert "rsi_index_committed" in kinds
    assert kinds.count("rsi_incident") == 1
    assert any(
        entry.evidence.get("kind") == "rsi_stage" and entry.evidence.get("stage") == "rollback"
        for entry in ledger.entries()
    )


def test_phase_c_does_not_set_flags_or_phase4() -> None:
    text = _PHASE.read_text(encoding="utf-8")
    assert "os.environ" not in text
    assert "phase4=True" not in text
    tree = ast.parse(text)
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
    assert "recertia.solver" not in modules
    assert not any(module.startswith("recertia.solver.") for module in modules)
