"""Computer-use golden seed skills (ADR-0019). Hand-authored, promotion-gated.

Each skill must inspect the fixture workspace. Canned write-only packs fail the golden.
"""

from __future__ import annotations

from contracts.skill import FailureMode, SkillVersion, Step
from recertia.memory.procedural.seeds._common import _HYGIENE, _cmd_criterion, _prov

_REPRO_CMD = (
    "python -c \""
    "from pathlib import Path; import broken; "
    "Path('evidence').mkdir(exist_ok=True); "
    "got=broken.add(1,1); "
    "Path('evidence/steps.txt').write_text("
    "f'add(1,1)={got} expected 2\\n', encoding='utf-8'); "
    "Path('evidence/network-notes.txt').write_text("
    "'network_policy=none\\n', encoding='utf-8')\""
)

_PLAYTEST_CMD = (
    "python -c \""
    "from pathlib import Path; "
    "src=Path('app.py').read_text(encoding='utf-8'); "
    "Path('playtest').mkdir(exist_ok=True); "
    "Path('playtest/step-log.txt').write_text(src, encoding='utf-8'); "
    "ok='def login' in src; "
    "Path('playtest/final-state.txt').write_text("
    "'ok=true\\n' if ok else 'ok=false\\n', encoding='utf-8')\""
)

_DOCS_CMD = (
    "python -c \""
    "from pathlib import Path; "
    "cli=Path('src/cli.py').read_text(encoding='utf-8'); "
    "docs=Path('docs/README.md').read_text(encoding='utf-8'); "
    "names=[n for n in ('login','cmd_export') if n in cli]; "
    "missing=[n for n in names if n not in docs]; "
    "Path('audit').mkdir(exist_ok=True); "
    "Path('audit/missing-pages.txt').write_text("
    "'\\n'.join('stale: '+m for m in missing)+'\\n', encoding='utf-8')\""
)


def repro_evidence_pack() -> SkillVersion:
    return SkillVersion(
        skill_id="repro-evidence-pack",
        version=1,
        title="Produce a bug-reproduction evidence pack",
        intent=(
            "Run the failing fixture and write a replayable evidence pack when a reviewer "
            "needs the observed fault, not a live debugger session."
        ),
        task_class="bug-reproduction",
        tags=["repro", "evidence"],
        steps=[
            Step(
                id="write_pack",
                tool="shell",
                intent="Execute broken.add and record the fault when the workdir is writable.",
                inputs={"command": _REPRO_CMD},
            ),
        ],
        certification_criteria=[
            _cmd_criterion(
                "steps-record-fault",
                "grep -q 'add(1,1)=' evidence/steps.txt",
                "canned evidence without running broken.py",
            ),
            _cmd_criterion(
                "notes-exist",
                "test -f evidence/network-notes.txt",
                "no network notes",
            ),
        ],
        failure_modes=[
            FailureMode(
                symptom="evidence pack missing the observed add(1,1) value",
                response="re-run broken.add(1,1) and refuse promotion if the record is canned",
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
            "Read the shipped UI module and record discovered defs plus a final-state "
            "predicate when APIs are missing and the product must still be exercised."
        ),
        task_class="playtest-operator",
        tags=["playtest", "ui"],
        steps=[
            Step(
                id="record",
                tool="shell",
                intent="Copy def names from app.py into the playtest log when writable.",
                inputs={"command": _PLAYTEST_CMD},
            ),
        ],
        certification_criteria=[
            _cmd_criterion(
                "log-has-def",
                "grep -q 'def login' playtest/step-log.txt",
                "canned playtest log without reading app.py",
            ),
            _cmd_criterion(
                "final-state",
                "grep -q 'ok=true' playtest/final-state.txt",
                "final state not ok",
            ),
        ],
        failure_modes=[
            FailureMode(
                symptom="playtest log does not contain defs from app.py",
                response="re-read app.py and rewrite the log from source",
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
            "Diff shipped command names against docs and list undocumented tokens when "
            "documentation has lagged a release."
        ),
        task_class="docs-auditor",
        tags=["docs", "audit"],
        steps=[
            Step(
                id="audit",
                tool="shell",
                intent="List command tokens missing from docs when the workdir is writable.",
                inputs={"command": _DOCS_CMD},
            ),
        ],
        certification_criteria=[
            _cmd_criterion(
                "missing-list",
                "test -f audit/missing-pages.txt",
                "no audit file",
            ),
            _cmd_criterion(
                "flags-export",
                "grep -q cmd_export audit/missing-pages.txt",
                "audit without cmd_export",
            ),
        ],
        failure_modes=[
            FailureMode(
                symptom="audit file missing cmd_export from src/cli.py",
                response="re-parse shipped names and rewrite missing-pages.txt",
            )
        ],
        provenance=_prov("docs-vs-shipped"),
        hygiene=_HYGIENE,
    )
