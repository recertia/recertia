"""The 46 dream keeps are candidates. The opened soak is not a pass."""

from __future__ import annotations

import sys
from pathlib import Path

from contracts.skill import SkillVersion
from contracts.status import SkillStatus
from recertia.ledger.hashchain import HashChainLedger

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

import dream46  # noqa: E402


def test_forty_six_keeps_are_candidates_and_the_soak_is_open() -> None:
    rows = dream46.catalog()
    assert len(rows) == 46
    assert set(dream46.EXCLUDED).isdisjoint(row["skill_id"] for row in rows)
    for row in rows:
        version_path = _ROOT / "skills" / row["skill_id"] / "v1" / "version.json"
        status_path = _ROOT / "skills" / row["skill_id"] / "v1" / "status.json"
        version = SkillVersion.model_validate_json(version_path.read_text(encoding="utf-8"))
        status = SkillStatus.model_validate_json(status_path.read_text(encoding="utf-8"))
        assert version.skill_id == row["skill_id"]
        assert len(version.certification_criteria) == len(row["checks"])
        assert status.lifecycle == "candidate"
        assert status.active is False
    skills = dream46.scored_skills()
    assert len(skills) == 46
    ledger = HashChainLedger(_ROOT / "docs" / "ops" / "soak-46" / "ledger.jsonl")
    soaks = [
        entry.evidence
        for entry in ledger.entries()
        if (entry.evidence or {}).get("kind") == "rsi_soak"
    ]
    assert len(soaks) >= 1
    soak = soaks[-1]
    assert soak["status"] == "open"
    assert soak["passed"] is False
    assert soak["aborted"] is False
    assert soak["decisions"] == 0
    assert soak["retirement_rows"] == 46
    assert soak["findings"] == []
    assert soak["default_on"] is False
    assert soak["default_on_scheduled"] is False
    assert soak["flags_default_changed"] is False
    assert soak["control_arm"]["measured"] is False
    assert soak["control_arm"]["noise_band"] == 0.05
    assert soak["blocking"] == "both"
    bands = [
        entry.evidence
        for entry in ledger.entries()
        if (entry.evidence or {}).get("kind") == "rsi_noise_band"
    ]
    assert len(bands) == 1
    assert bands[0]["noise_band"] == 0.05
