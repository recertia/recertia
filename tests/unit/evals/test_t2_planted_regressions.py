"""Structural lock for the T2 planted-regression set (REC-013). Does not run the gate."""

from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
SET_DIR = REPO / "evals" / "planted-regressions" / "t2"
SKILLS = REPO / "skills"
CASES = (
    "add-editorconfig",
    "add-gitignore-entry",
    "add-license-mit",
    "add-makefile-target",
    "add-pytest-config",
    "add-readme-section",
    "bump-action-checkout",
    "ensure-src-layout",
    "pin-python-version",
    "strip-trailing-whitespace",
)


def test_ten_cases_are_steps_only_v2_successors() -> None:
    for skill_id in CASES:
        v1 = json.loads((SKILLS / skill_id / "v1" / "version.json").read_text(encoding="utf-8"))
        v2 = json.loads((SET_DIR / skill_id / "version.json").read_text(encoding="utf-8"))
        assert v2["skill_id"] == skill_id
        assert v2["version"] == 2
        assert v2["supersedes"] == 1
        assert v1["certification_criteria"] == v2["certification_criteria"]
        assert v1["steps"][0]["inputs"]["command"] != v2["steps"][0]["inputs"]["command"]
        skipped = {"version", "supersedes", "steps"}
        for key, value in v1.items():
            if key in skipped:
                continue
            assert v2[key] == value, f"{skill_id} changed {key}"
        diff = (SET_DIR / skill_id / "criteria.diff").read_text(encoding="utf-8")
        assert "--- v1 certification_criteria" in diff
        assert "+++" in diff
        assert "@@" not in diff


def test_manifest_covers_the_set_except_itself() -> None:
    manifest = SET_DIR / "MANIFEST.sha256"
    assert manifest.is_file()
    listed = {
        line.split("  ", 1)[1].strip()
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    on_disk = {
        path.relative_to(SET_DIR).as_posix()
        for path in SET_DIR.rglob("*")
        if path.is_file() and path.name != "MANIFEST.sha256"
    }
    assert listed == on_disk
    assert "PREFREEZE-REFUSALS.json" in listed
    for skill_id in CASES:
        assert f"{skill_id}/version.json" in listed
        assert f"{skill_id}/criteria.diff" in listed


def test_prefreeze_refusals_are_allowed_kinds_on_container() -> None:
    payload = json.loads((SET_DIR / "PREFREEZE-REFUSALS.json").read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in payload["cases"]}
    assert tuple(by_id) == CASES or set(by_id) == set(CASES)
    allowed = {"golden_gate", "predecessor_non_regression"}
    for skill_id in CASES:
        row = by_id[skill_id]
        assert row["refusal_kind"] in allowed, skill_id
        assert row["backend_used"] == "container", skill_id
        assert row["criteria_diff"] == "identical", skill_id
        if row.get("attempts_measured") is True:
            assert isinstance(row["attempts"], int), skill_id
            assert row["attempts"] >= 1, skill_id
        else:
            assert row["attempts"] is None, skill_id


def _load_prefreeze() -> ModuleType:
    spec = importlib.util.spec_from_file_location("t2_prefreeze", SET_DIR / "prefreeze.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # No __pycache__ in the frozen set: the manifest test compares every file on disk.
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def test_prefreeze_refusals_do_not_present_attempts_as_a_measurement() -> None:
    """The pre-freeze run never measured attempts; no row may read as if it had (REC-013)."""

    payload = json.loads((SET_DIR / "PREFREEZE-REFUSALS.json").read_text(encoding="utf-8"))
    rows = payload["cases"]
    assert rows
    unmeasured = []
    for row in rows:
        skill_id = row["id"]
        assert "attempts_measured" in row, f"{skill_id}: attempts has no measured/not-measured flag"
        assert isinstance(row["attempts_measured"], bool), skill_id
        if row["attempts_measured"]:
            assert isinstance(row["attempts"], int) and row["attempts"] >= 1, skill_id
        else:
            assert row["attempts"] is None, f"{skill_id}: unmeasured attempts carries a number"
            unmeasured.append(skill_id)
    if unmeasured:
        reason = payload.get("not_measured", {}).get("attempts")
        assert isinstance(reason, str) and reason.strip(), "no top-level not_measured.attempts reason"


def test_prefreeze_case_rows_mark_attempts_not_measured() -> None:
    """Rows the pre-freeze script would write carry no attempt count it did not measure."""

    prefreeze = _load_prefreeze()
    row = prefreeze.case_row(
        skill_id="add-license-mit",
        refusal_kind="predecessor_non_regression",
        backend_used="container",
        criteria_diff="identical",
        executor_id="recertia.solver.executor.ContainerExecutor",
        code_sha="0" * 40,
        date_pt="2026-10-21",
        message="predecessor non-regression failed",
    )
    assert row["attempts"] is None
    assert row["attempts_measured"] is False
    assert prefreeze.case_row_problems(row) == []
    assert prefreeze.case_row_problems({**row, "attempts": 1}) != []


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_manifest_hashes_match_the_files_on_disk() -> None:
    """M1: every MANIFEST.sha256 entry is the sha256 of that file, so no frozen file can change silently."""

    rows = [
        line.split("  ", 1)
        for line in (SET_DIR / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    names = [name for _, name in rows]
    assert len(names) == len(set(names)), "duplicate MANIFEST.sha256 entries"
    mismatched = [
        name for digest, name in rows if not (SET_DIR / name).is_file() or _sha256(SET_DIR / name) != digest
    ]
    assert mismatched == [], f"MANIFEST.sha256 does not match: {mismatched}"


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_frozen_results_contain_no_absolute_temp_paths() -> None:
    """M3: the frozen PREFREEZE results are path-independent (no scratch or temp dirs)."""

    temp_roots = {"/tmp", "/var/tmp", "/private/var", "/var/folders", tempfile.gettempdir()}
    temp_path = re.compile(
        r"(?:^|[\s'\"=(:\[,])(?:" + "|".join(re.escape(root) for root in sorted(temp_roots)) + r")/"
    )
    payload = json.loads((SET_DIR / "PREFREEZE-REFUSALS.json").read_text(encoding="utf-8"))
    leaks = [text for text in _strings(payload) if temp_path.search(text) or "recertia-t2-prefreeze-" in text]
    assert leaks == [], f"absolute temp paths in PREFREEZE-REFUSALS.json: {leaks}"


_GATE_KINDS = {"golden gate": "golden_gate", "predecessor non-regression": "predecessor_non_regression"}


def _refusal(exc: Exception) -> tuple[str | None, int | None]:
    """Refusal kind and failing-fixture count, for gates before and after REC-009."""

    kind = getattr(exc, "refusal_kind", None)
    count = getattr(exc, "failing_count", None)
    message = str(exc)
    if kind is None:
        kind = next((v for k, v in _GATE_KINDS.items() if k in message.lower()), None)
    named = re.search(r"failing fixtures=(\[[^\]]*\])", message)
    if named is not None:
        count = len(json.loads(named.group(1).replace("'", '"')))
    return kind, count


def _promote_v2(tmp: Path, skill_id: str, v2: dict[str, Any]) -> Exception | None:
    from contracts.skill import SkillVersion
    from recertia.memory.procedural.promote import PromotionError, promote_to_approved
    from recertia.memory.procedural.store import SkillStore
    from recertia.review.gate import default_golden_factory

    shutil.copytree(SKILLS / skill_id / "v1", tmp / "skills" / skill_id / "v1")
    store = SkillStore(tmp / "skills")
    store.write_candidate(SkillVersion.model_validate(v2))
    kwargs: dict[str, Any] = {}
    if "audit" in inspect.signature(promote_to_approved).parameters:  # REC-009 and later
        audit_mod = importlib.import_module("recertia.review.promotion_audit")
        kwargs["audit"] = audit_mod.OperatorAuditStore(tmp / "operator-audit")
    try:
        status = promote_to_approved(
            store,
            skill_id,
            2,
            golden_dir=REPO / "evals" / "golden" / "repo-chore" / skill_id,
            golden_root=REPO / "evals" / "golden",
            runs_root=tmp / "runs",
            log_dir=tmp / "logs",
            repo_root=REPO,
            factory=default_golden_factory(),
            **kwargs,
        )
    except PromotionError as exc:
        refusal: Exception = exc
        return refusal
    assert status.lifecycle == "approved"
    return None


@pytest.mark.parametrize("skill_id", CASES)
def test_gate_refuses_each_plant_and_approves_its_v1_control(
    skill_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2: the real golden gate (local backend) refuses each plant as frozen, and approves the
    same v2 envelope with v1's steps restored, so the refusal is caused by the plant."""

    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "local")
    monkeypatch.setenv("RECERTIA_GOLDEN_BACKEND", "local")
    monkeypatch.setenv("RECERTIA_MODEL_PROVIDER", "stub")
    monkeypatch.setenv("RECERTIA_MODEL_ID", "stub")
    monkeypatch.setenv("PATH", os.pathsep.join([str(Path(sys.executable).parent), os.environ["PATH"]]))
    frozen = {
        row["id"]: row
        for row in json.loads((SET_DIR / "PREFREEZE-REFUSALS.json").read_text(encoding="utf-8"))["cases"]
    }[skill_id]

    plant = json.loads((SET_DIR / skill_id / "version.json").read_text(encoding="utf-8"))
    (tmp_path / "plant").mkdir()
    refused = _promote_v2(tmp_path / "plant", skill_id, plant)
    assert refused is not None, f"{skill_id}: plant was APPROVED by the gate"
    kind, count = _refusal(refused)
    assert kind == frozen["refusal_kind"], (
        f"{skill_id}: gate says {kind}, frozen says {frozen['refusal_kind']}"
    )
    assert count == frozen["failing_count"], f"{skill_id}: gate failing count {count} != frozen"

    control = copy.deepcopy(plant)
    control["steps"] = json.loads((SKILLS / skill_id / "v1" / "version.json").read_text(encoding="utf-8"))[
        "steps"
    ]
    (tmp_path / "control").mkdir()
    assert _promote_v2(tmp_path / "control", skill_id, control) is None, (
        f"{skill_id}: v1-step control refused"
    )


def test_golden_fixtures_match_head() -> None:
    proc = subprocess.run(
        ["git", "diff", "--exit-code", "HEAD", "--", "evals/golden"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
