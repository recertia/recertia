"""Synthetic promotion fixtures shared by the REC-009 leak and held-out tests.

The skill and the fixture have different names (skill ``leak-probe``, failing fixture
dir ``HELDOUT-x``), so "absent" means ``HELDOUT-x`` and its golden path occur nowhere in
proposer-visible output. Everything is built under the caller's ``tmp_path``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from contracts.criteria import SkillCertificationCriterion, TaskCriterion, mint_rejecting_proof
from contracts.skill import FailureMode, Hygiene, Provenance, SkillVersion, Step

SKILL = "leak-probe"
FIXTURE = "HELDOUT-x"
_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
ABS_PATH = re.compile(r"(?:^|[\s'\"=(:\[,])/[^\s'\"/]+/")


def proven_criterion(cid: str, command: str) -> SkillCertificationCriterion:
    base = SkillCertificationCriterion(id=cid, kind="command", run=command, preregistered=True)
    return base.model_copy(
        update={"sensitivity_proof": mint_rejecting_proof(base, fingerprint=f"leak-{cid}")}
    )


def leak_skill(version: int, *, command: str, criterion_run: str) -> SkillVersion:
    return SkillVersion(
        skill_id=SKILL,
        version=version,
        supersedes=version - 1 if version > 1 else None,
        title=f"Leak probe skill v{version}",
        intent=f"Synthetic skill used only by the REC-009 leak test, version {version}.",
        task_class="repo-chore",
        steps=[
            Step(
                id="step_1",
                tool="shell",
                intent="Run the synthetic fixture command",
                inputs={"command": command},
            )
        ],
        certification_criteria=[proven_criterion("ok", criterion_run)],
        failure_modes=[
            FailureMode(
                symptom="Fixture command exits non-zero on a dirty workspace",
                response="Restore the snapshot and re-run the fixture command",
            )
        ],
        provenance=Provenance(
            distilled_from_run="leak-probe",
            distilled_at=_NOW,
            curation="human_authored",
            authoring_prior_version="ap-test",
        ),
        hygiene=Hygiene(secret_scan="passed", scanned_at=_NOW),
    )


def leak_golden(
    path: Path,
    *,
    command: str,
    workspace_file: str | None = None,
    request: str | None = None,
) -> Path:
    criterion = TaskCriterion(
        id="gate",
        kind="command",
        run=command,
        source="caller",
        sensitivity_proof=mint_rejecting_proof(
            TaskCriterion(id="gate", kind="command", run=command, source="caller"),
            fingerprint="leak-task-gate",
        ),
    )
    (path / "workspace").mkdir(parents=True)
    if workspace_file:
        (path / "workspace" / workspace_file).write_text("x\n", encoding="utf-8")
    (path / "task.json").write_text(
        json.dumps(
            {
                "request": request if request is not None else f"satisfy {command}",
                "expected_skill_id": SKILL,
                "criteria": [criterion.model_dump(mode="json")],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (path / "expect.json").write_text('{"terminal": "solved"}\n', encoding="utf-8")
    return path


def assert_no_leak(text: str, *paths: Path) -> None:
    assert FIXTURE not in text
    for path in paths:
        assert str(path) not in text
    assert not ABS_PATH.search(text), f"filesystem path in proposer output: {text!r}"


def exception_surfaces(exc: BaseException) -> list[str]:
    """str, repr, args, every attribute and notes; the exception must not chain."""

    surfaces = [str(exc), repr(exc), repr(exc.args)]
    surfaces.extend(repr(v) for v in vars(exc).values())
    surfaces.append(repr(getattr(exc, "__notes__", [])))
    assert exc.__cause__ is None and exc.__context__ is None
    decision = getattr(exc, "decision", None)
    if callable(decision):
        surfaces.append(json.dumps(decision()))
    return surfaces
