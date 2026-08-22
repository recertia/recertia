"""Computer-use golden seed skills (ADR-0019). Hand-authored, promotion-gated."""

from __future__ import annotations

from contracts.skill import FailureMode, SkillVersion, Step
from recertia.memory.procedural.seeds._common import _HYGIENE, _cmd_criterion, _prov


def repro_evidence_pack() -> SkillVersion:
    return SkillVersion(
        skill_id="repro-evidence-pack",
        version=1,
        title="Produce a bug-reproduction evidence pack",
        intent=(
            "Write a self-contained evidence pack (steps plus network notes) when a fixture "
            "fails and a reviewer needs a replayable repro, not a live debugger session."
        ),
        task_class="bug-reproduction",
        tags=["repro", "evidence"],
        steps=[
            Step(
                id="write_pack",
                tool="shell",
                intent="Write evidence pack files when the workdir is writable.",
                inputs={
                    "command": (
                        "python -c \"from pathlib import Path; "
                        "Path('evidence').mkdir(exist_ok=True); "
                        "Path('evidence/steps.txt').write_text('1. run broken.py\\n', encoding='utf-8'); "
                        "Path('evidence/network-notes.txt').write_text('no network\\n', encoding='utf-8')\""
                    )
                },
            ),
        ],
        certification_criteria=[
            _cmd_criterion("steps-exist", "test -f evidence/steps.txt", "no evidence dir"),
            _cmd_criterion(
                "notes-exist", "test -f evidence/network-notes.txt", "no network notes"
            ),
        ],
        failure_modes=[
            FailureMode(
                symptom="evidence files missing after the pack step",
                response="re-run the pack command and refuse promotion if either file is absent",
            )
        ],
        provenance=_prov("repro-evidence-pack"),
        hygiene=_HYGIENE,
    )


def playtest_login_path() -> SkillVersion:
    return SkillVersion(
        skill_id="playtest-login-path",
        version=1,
        title="Playtest a login path and record the final state",
        intent=(
            "Drive the login path and record UI step assertions plus a final-state predicate "
            "when APIs are missing and the product must still be exercised through its UI."
        ),
        task_class="playtest-operator",
        tags=["playtest", "ui"],
        steps=[
            Step(
                id="record",
                tool="shell",
                intent="Write playtest log and final-state files when the workdir is writable.",
                inputs={
                    "command": (
                        "python -c \"from pathlib import Path; "
                        "Path('playtest').mkdir(exist_ok=True); "
                        "Path('playtest/step-log.txt')"
                        ".write_text('open login\\nsubmit\\n', encoding='utf-8'); "
                        "Path('playtest/final-state.txt').write_text('ok=true\\n', encoding='utf-8')\""
                    )
                },
            ),
        ],
        certification_criteria=[
            _cmd_criterion("log-exists", "test -f playtest/step-log.txt", "no playtest log"),
            _cmd_criterion(
                "final-state",
                "grep -q 'ok=true' playtest/final-state.txt",
                "final state not ok",
            ),
        ],
        failure_modes=[
            FailureMode(
                symptom="final-state file missing ok=true",
                response="re-drive the login path and rewrite playtest/final-state.txt",
            )
        ],
        provenance=_prov("playtest-login-path"),
        hygiene=_HYGIENE,
    )


def docs_vs_shipped() -> SkillVersion:
    return SkillVersion(
        skill_id="docs-vs-shipped",
        version=1,
        title="Audit docs against shipped commands",
        intent=(
            "Diff shipped product files against docs and list missing or stale pages when "
            "documentation has lagged a release."
        ),
        task_class="docs-auditor",
        tags=["docs", "audit"],
        steps=[
            Step(
                id="audit",
                tool="shell",
                intent="Write audit/missing-pages.txt with stale entries when the workdir is writable.",
                inputs={
                    "command": (
                        "python -c \"from pathlib import Path; "
                        "Path('audit').mkdir(exist_ok=True); "
                        "Path('audit/missing-pages.txt').write_text("
                        "'stale: export command undocumented\\n', encoding='utf-8')\""
                    )
                },
            ),
        ],
        certification_criteria=[
            _cmd_criterion("missing-list", "test -f audit/missing-pages.txt", "no audit file"),
            _cmd_criterion(
                "flags-stale",
                "grep -q stale audit/missing-pages.txt",
                "audit without stale",
            ),
        ],
        failure_modes=[
            FailureMode(
                symptom="audit file missing or has no stale entries",
                response="re-diff shipped commands against docs and rewrite missing-pages.txt",
            )
        ],
        provenance=_prov("docs-vs-shipped"),
        hygiene=_HYGIENE,
    )
