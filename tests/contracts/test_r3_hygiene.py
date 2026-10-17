"""R3 contract-CI checks: cross-refs, milestone deps, assumptions hygiene."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.check_assumptions_hygiene import check as check_assumptions  # noqa: E402
from scripts.check_cross_refs import check as check_cross_refs  # noqa: E402
from scripts.check_milestone_deps import check as check_milestone_deps  # noqa: E402


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / script), "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def test_cross_refs_script_passes() -> None:
    result = _run("check_cross_refs.py")
    assert result.returncode == 0, result.stderr or result.stdout


def test_milestone_deps_script_passes() -> None:
    result = _run("check_milestone_deps.py")
    assert result.returncode == 0, result.stderr or result.stdout


def test_assumptions_hygiene_script_passes() -> None:
    result = _run("check_assumptions_hygiene.py")
    assert result.returncode == 0, result.stderr or result.stdout


def test_raw_writes_inventory_matches_baseline() -> None:
    result = _run("check_raw_writes.py")
    assert result.returncode == 0, result.stderr or result.stdout


def test_assumptions_hygiene_flags_unmarked_gate(tmp_path: Path) -> None:
    assumptions = tmp_path / "assumptions.md"
    assumptions.write_text(
        "## a1. Claim\n\n- **Status:** `untested` — no evidence\n",
        encoding="utf-8",
    )
    plan = tmp_path / "plan.md"
    plan.write_text(
        "## M4 — x\n\n**Done when:** must achieve a1 positive lift on traffic.\n",
        encoding="utf-8",
    )
    errors = check_assumptions(plan, assumptions)
    assert errors and "a1" in errors[0]


def test_milestone_deps_flags_early_symbol(tmp_path: Path) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text(
        "## M1 — memory\n\n**Done when:** JobRunner mines skills and mean_composition_depth rises.\n",
        encoding="utf-8",
    )
    errors = check_milestone_deps(plan)
    assert any("JobRunner" in e for e in errors)


def test_cross_refs_flags_dangling(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("[missing](nope.md#gone)\n", encoding="utf-8")
    errors = check_cross_refs(docs)
    assert errors


def test_cross_refs_flags_incomplete_split_index(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "architecture.md").write_text("# Architecture\n", encoding="utf-8")
    errors = check_cross_refs(docs)
    assert any("missing split-document topic" in error for error in errors)
