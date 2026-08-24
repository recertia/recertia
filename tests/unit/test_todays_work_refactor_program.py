"""The 2026-08-24 landing-extract Goal pack must stay a valid MigrationProgram."""

from __future__ import annotations

from pathlib import Path

from contracts.program import MigrationProgram

REPO = Path(__file__).resolve().parents[2]
PACK = REPO / "docs/plans/2026-08-24-todays-work-refactor.program.json"
PROMPT = REPO / "docs/plans/2026-08-24-todays-work-refactor.md"


def test_todays_work_refactor_program_validates() -> None:
    prog = MigrationProgram.model_validate_json(PACK.read_text(encoding="utf-8"))
    assert prog.program_id == "gp-2026-08-24-todays-work-refactor"
    assert prog.decomposition == "by_seam"
    assert prog.task_class == "repo-chore"
    assert [s.step_id for s in prog.steps] == [
        "characterize",
        "honesty",
        "extract",
        "lock",
    ]
    assert [s.role for s in prog.steps] == [
        "characterization",
        "custom",
        "structural",
        "behaviour_lock",
    ]
    assert PROMPT.is_file()
    text = PROMPT.read_text(encoding="utf-8")
    assert "Paste into a Cloud Agent" in text
    assert "RW-GA" in text
