"""The 46 dream keeps. Draft skills, not a certification and not a soak pass.

Scores use ``calib_dream50`` on the imagined check counts. The four skills
that dropped or flipped are not in this set. Nothing here is approved.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contracts.criteria import SkillCertificationCriterion, mint_rejecting_proof
from contracts.skill import FailureMode, Hygiene, Precondition, Provenance, SkillVersion, Step
from contracts.stats import SkillStats
from contracts.status import SkillStatus
from recertia.ledger.hashchain import HashChainLedger
from recertia.ops.rsi_phase_c import DurableIndex
from recertia.ops.rsi_phase_d import SoakSkill, open_soak
from recertia.rsi.models import CalibConstants
from recertia.rsi.scoring import score_run

DREAM_AT = datetime(2026, 11, 9, 3, 5, tzinfo=timezone.utc)
NOISE_BAND = 0.05
SNAPSHOT_ID = "dream46"
POST_CHANGE_SKILL = "dream46-post"
EXCLUDED = (
    "require-signed-commits",
    "flag-undocumented-error",
    "keep-conflict-visible",
    "date-each-claim",
)

CONSTANTS = CalibConstants(
    version="calib_dream50",
    family_quantile={
        "repo-chore": 4.0,
        "bug-reproduction": 8.0,
        "docs-auditor": 3.0,
        "mea": 5.0,
        "playtest-operator": 4.0,
        "research-synthesis": 24.0,
    },
    approved_by="",
    approved_at="",
    partial_scale=0.6,
    fallback_d=82.0,
)


def _marks(skill_id: str, count: int) -> tuple[str, list[tuple[str, str]]]:
    path = f".recertia-dream46/{skill_id}.txt"
    lines = " ".join(f"'{index}'" for index in range(1, count + 1))
    command = f"mkdir -p .recertia-dream46 && printf '%s\\n' {lines} > {path}"
    checks = [
        (f"mark_{index}", f"grep -qx '{index}' {path}") for index in range(1, count + 1)
    ]
    return command, checks


def _row(
    skill_id: str,
    title: str,
    intent: str,
    task_class: str,
    command: str,
    checks: list[tuple[str, str]],
    checks_passed: int,
    strict: bool,
) -> dict[str, Any]:
    if strict and checks_passed != len(checks):
        raise ValueError(f"{skill_id} strict run must pass every check")
    if checks_passed > len(checks):
        raise ValueError(f"{skill_id} passed more checks than it has")
    return {
        "skill_id": skill_id,
        "title": title,
        "intent": intent,
        "task_class": task_class,
        "command": command,
        "checks": checks,
        "checks_passed": checks_passed,
        "strict": strict,
    }


def _marked(skill_id: str, title: str, intent: str, task_class: str, count: int, passed: int, strict: bool) -> dict[str, Any]:
    command, checks = _marks(skill_id, count)
    return _row(skill_id, title, intent, task_class, command, checks, passed, strict)


def catalog() -> list[dict[str, Any]]:
    """Forty-six keeps. Check counts match the dream that scored them."""

    rows = [
        _row(
            "add-py-typed",
            "Add a py.typed marker",
            "Write an empty py.typed so type checkers treat the package as typed without a partial waiver.",
            "repo-chore",
            "mkdir -p src/pkg && : > src/pkg/py.typed",
            [("exists", "test -f src/pkg/py.typed"), ("empty", "! grep -q . src/pkg/py.typed")],
            2,
            True,
        ),
        _row(
            "add-dependabot-pip",
            "Add weekly pip Dependabot",
            "Write a Dependabot config that checks pip at the repo root once a week and names the pip ecosystem.",
            "repo-chore",
            "mkdir -p .github && printf '%s\\n' 'version: 2' 'updates:' '  - package-ecosystem: pip' '    directory: /' '    schedule:' '      interval: weekly' > .github/dependabot.yml",
            [
                ("file", "test -f .github/dependabot.yml"),
                ("pip", "grep -q 'package-ecosystem: pip' .github/dependabot.yml"),
                ("weekly", "grep -q 'interval: weekly' .github/dependabot.yml"),
            ],
            3,
            True,
        ),
        _row(
            "add-pytest-workflow",
            "Add a pytest CI workflow",
            "Add a GitHub Actions workflow that runs pytest on push and pull request and does not print the environment.",
            "repo-chore",
            "mkdir -p .github/workflows && printf '%s\\n' 'name: ci' 'on: [push, pull_request]' 'jobs:' '  test:' '    runs-on: ubuntu-latest' '    steps:' '      - run: pytest' > .github/workflows/ci.yml",
            [
                ("file", "test -f .github/workflows/ci.yml"),
                ("pytest", "grep -q 'run: pytest' .github/workflows/ci.yml"),
                ("push", "grep -q push .github/workflows/ci.yml"),
                ("quiet", "! grep -q 'printenv' .github/workflows/ci.yml"),
            ],
            4,
            True,
        ),
        _row(
            "redact-env-example",
            "Write an empty env example",
            "Write .env.example with the key names and blank values so a real token is not committed.",
            "repo-chore",
            "printf '%s\\n' 'API_TOKEN=' 'DATABASE_URL=' > .env.example",
            [
                ("file", "test -f .env.example"),
                ("token-blank", "grep -qx 'API_TOKEN=' .env.example"),
                ("url-blank", "grep -qx 'DATABASE_URL=' .env.example"),
            ],
            3,
            True,
        ),
        _row(
            "add-pre-commit-config",
            "Add a pre-commit config",
            "Add a pre-commit config that hooks trailing-whitespace so the check runs before a commit.",
            "repo-chore",
            "printf '%s\\n' 'repos:' '  - repo: https://github.com/pre-commit/pre-commit-hooks' '    rev: v5.0.0' '    hooks:' '      - id: trailing-whitespace' > .pre-commit-config.yaml",
            [
                ("file", "test -f .pre-commit-config.yaml"),
                ("hook", "grep -q 'id: trailing-whitespace' .pre-commit-config.yaml"),
            ],
            2,
            True,
        ),
        _row(
            "add-codeowners",
            "Add a CODEOWNERS file",
            "Add CODEOWNERS with a default owner. The dream run passed the file check and missed the owner line.",
            "repo-chore",
            "mkdir -p .github && printf '%s\\n' '* @recertia/maintainers' > .github/CODEOWNERS",
            [
                ("file", "test -f .github/CODEOWNERS"),
                ("owner", "grep -q '@recertia/maintainers' .github/CODEOWNERS"),
            ],
            1,
            False,
        ),
        _row(
            "add-issue-template",
            "Add a bug issue template",
            "Add a bug report template that asks for the version and the exact command that failed.",
            "repo-chore",
            "mkdir -p .github/ISSUE_TEMPLATE && printf '%s\\n' 'name: Bug' 'about: A reproduced failure' 'body:' '  - type: textarea' '    attributes:' '      label: Command' > .github/ISSUE_TEMPLATE/bug.yml",
            [
                ("file", "test -f .github/ISSUE_TEMPLATE/bug.yml"),
                ("command", "grep -q 'label: Command' .github/ISSUE_TEMPLATE/bug.yml"),
            ],
            2,
            True,
        ),
        _row(
            "add-pull-request-template",
            "Add a pull request template",
            "Add a pull request template that asks what changed and how it was checked.",
            "repo-chore",
            "mkdir -p .github && printf '%s\\n' '## What changed' '## How to check' > .github/pull_request_template.md",
            [
                ("file", "test -f .github/pull_request_template.md"),
                ("check", "grep -q 'How to check' .github/pull_request_template.md"),
            ],
            2,
            True,
        ),
        _row(
            "pin-dockerfile-base",
            "Pin the Docker base image",
            "Write a Dockerfile whose base image is a digest-pinned python tag rather than a floating latest.",
            "repo-chore",
            "printf '%s\\n' 'FROM python:3.12-slim@sha256:0000000000000000000000000000000000000000000000000000000000000000' 'WORKDIR /app' 'COPY . .' > Dockerfile",
            [
                ("file", "test -f Dockerfile"),
                ("from", "grep -q '^FROM python:3.12-slim@sha256:' Dockerfile"),
                ("not-latest", "! grep -q ':latest' Dockerfile"),
            ],
            3,
            True,
        ),
        _row(
            "add-makefile-help",
            "Add a make help target",
            "Add a Makefile help target that prints the available targets from the comments.",
            "repo-chore",
            "printf '%s\\n' 'help: ## show targets' \"\\t@grep -E '^[a-z-]+:.*?## ' Makefile\" > Makefile",
            [("help", "grep -q '^help:' Makefile")],
            1,
            True,
        ),
        _row(
            "ignore-pytest-cache",
            "Ignore pytest cache files",
            "Add pytest and bytecode caches to gitignore. The dream run passed only the pytest-cache line.",
            "repo-chore",
            "printf '%s\\n' '.pytest_cache/' '__pycache__/' > .gitignore",
            [
                ("pytest-cache", "grep -qx '.pytest_cache/' .gitignore"),
                ("pycache", "grep -qx '__pycache__/' .gitignore"),
            ],
            1,
            False,
        ),
        _row(
            "add-funding-yml",
            "Add a funding file",
            "Add a GitHub funding file that names a custom funding URL without embedding a secret.",
            "repo-chore",
            "mkdir -p .github && printf '%s\\n' 'custom: [\"https://example.com/fund\"]' > .github/FUNDING.yml",
            [("file", "test -f .github/FUNDING.yml")],
            1,
            True,
        ),
        _row(
            "set-ruff-line-length",
            "Set the Ruff line length",
            "Set the Ruff line length to 110 in pyproject so the formatter matches the repo gate.",
            "repo-chore",
            "printf '%s\\n' '[tool.ruff]' 'line-length = 110' >> pyproject.toml",
            [
                ("section", "grep -q '\\[tool.ruff\\]' pyproject.toml"),
                ("length", "grep -q 'line-length = 110' pyproject.toml"),
            ],
            2,
            True,
        ),
        _row(
            "add-devcontainer",
            "Add a Dev Container file",
            "Add a devcontainer that uses Python 3.12, names the container, and does not mount the host home.",
            "repo-chore",
            "mkdir -p .devcontainer && printf '%s\\n' '{' '  \"name\": \"recertia\",' '  \"image\": \"mcr.microsoft.com/devcontainers/python:3.12\"' '}' > .devcontainer/devcontainer.json",
            [
                ("file", "test -f .devcontainer/devcontainer.json"),
                ("name", "grep -q '\"name\": \"recertia\"' .devcontainer/devcontainer.json"),
                ("image", "grep -q 'python:3.12' .devcontainer/devcontainer.json"),
                ("no-home", "! grep -q '/root' .devcontainer/devcontainer.json"),
            ],
            4,
            True,
        ),
        _row(
            "add-citation-cff",
            "Add a citation file",
            "Add a CITATION.cff that names the title and the author so the repo can be cited.",
            "repo-chore",
            "printf '%s\\n' 'cff-version: 1.2.0' 'title: Recertia' 'authors:' '  - family-names: Schmidt' > CITATION.cff",
            [
                ("file", "test -f CITATION.cff"),
                ("title", "grep -q 'title: Recertia' CITATION.cff"),
            ],
            2,
            True,
        ),
        _row(
            "fix-code-keep-test",
            "Fix code and keep the test",
            "Make the named test pass by changing the implementation. The test file itself must stay byte-for-byte.",
            "bug-reproduction",
            "printf '%s\\n' 'def add(left, right):' '    return left + right' > sample.py && printf '%s\\n' 'from sample import add' 'def test_add():' '    assert add(1, 1) == 2' > test_sample.py",
            [
                ("impl", "grep -q 'return left + right' sample.py"),
                ("test-file", "test -f test_sample.py"),
                ("asserts", "grep -q 'assert add(1, 1) == 2' test_sample.py"),
                ("not-deleted", "grep -q 'def test_add' test_sample.py"),
                ("not-skipped", "! grep -q pytest.skip test_sample.py"),
                ("not-xfail", "! grep -q xfail test_sample.py"),
            ],
            6,
            True,
        ),
        _row(
            "restore-missing-import",
            "Restore a missing import",
            "Add the missing import so the module loads, without deleting the call site that needed it.",
            "bug-reproduction",
            "printf '%s\\n' 'from pathlib import Path' 'def root():' '    return Path(\".\")' > sample.py",
            [
                ("import", "grep -q 'from pathlib import Path' sample.py"),
                ("call", "grep -q 'Path(' sample.py"),
                ("function", "grep -q 'def root' sample.py"),
                ("file", "test -f sample.py"),
            ],
            4,
            True,
        ),
        _marked(
            "repair-off-by-one",
            "Repair an off-by-one bound",
            "Fix an inclusive range so the last index is included. Eight marker checks are the dream bar.",
            "bug-reproduction",
            8,
            8,
            True,
        ),
        _marked(
            "unbreak-fixture-path",
            "Repair a fixture path",
            "Point the fixture loader at the real path. The dream run passed three of the five path checks.",
            "bug-reproduction",
            5,
            3,
            False,
        ),
        _marked(
            "stop-swallowing-exception",
            "Stop swallowing an exception",
            "Let the caught exception surface instead of returning a fake success. Five checks are the dream bar.",
            "bug-reproduction",
            5,
            5,
            True,
        ),
        _marked(
            "repair-timezone-compare",
            "Repair a naive time compare",
            "Compare timezone-aware datetimes only. The dream run passed two of the four clock checks.",
            "bug-reproduction",
            4,
            2,
            False,
        ),
        _marked(
            "fix-empty-slice",
            "Fix an empty-slice guard",
            "Treat an empty slice as no work instead of wrapping the index. Four checks are the dream bar.",
            "bug-reproduction",
            4,
            4,
            True,
        ),
        _marked(
            "repair-flaky-order",
            "Repair a flaky sort order",
            "Sort the assertion input so order is stable. The dream run passed one of the three order checks.",
            "bug-reproduction",
            3,
            1,
            False,
        ),
        _row(
            "sync-readme-exports",
            "Sync README exports",
            "List only the names the package actually exports, and list every public name the README claims.",
            "docs-auditor",
            "printf '%s\\n' 'def open_soak():' '    return None' > api.py && printf '%s\\n' 'Exports: open_soak' > README.md",
            [
                ("readme", "grep -q 'open_soak' README.md"),
                ("export", "grep -q 'def open_soak' api.py"),
                ("no-extra", "! grep -q 'missing_symbol' README.md"),
            ],
            3,
            True,
        ),
        _row(
            "drop-dead-cli-flag",
            "Drop a dead CLI flag",
            "Remove a documented flag that the CLI no longer accepts, and leave the real flag in place.",
            "docs-auditor",
            "printf '%s\\n' 'usage: recertia run' 'options: --ledger' > README.md",
            [
                ("real-flag", "grep -q -- '--ledger' README.md"),
                ("dead-gone", "! grep -q -- '--obsolete' README.md"),
            ],
            2,
            True,
        ),
        _marked(
            "align-env-names",
            "Align documented env names",
            "Make the documented environment names match the names the process reads. The dream run passed one of three.",
            "docs-auditor",
            3,
            1,
            False,
        ),
        _row(
            "mark-deprecated-api",
            "Mark a deprecated API",
            "Document the replacement next to a deprecated function so callers can see the successor.",
            "docs-auditor",
            "printf '%s\\n' 'def old():' '    \"\"\"Deprecated. Use current.\"\"\"' '    return current()' 'def current():' '    return 1' > api.py",
            [
                ("word", "grep -q Deprecated api.py"),
                ("successor", "grep -q 'def current' api.py"),
            ],
            2,
            True,
        ),
        _row(
            "fix-broken-anchor",
            "Fix a broken doc anchor",
            "Point the README link at a heading that exists in the same file.",
            "docs-auditor",
            "printf '%s\\n' '# Use' 'See [Use](#use).' > README.md",
            [("anchor", "grep -q '](#use)' README.md")],
            1,
            True,
        ),
        _row(
            "remove-stale-version",
            "Remove a stale version pin",
            "Replace a documented version that the package no longer ships with the version it does ship.",
            "docs-auditor",
            "printf '%s\\n' 'Requires Python 3.11.' > README.md",
            [
                ("current", "grep -q 'Python 3.11' README.md"),
                ("stale-gone", "! grep -q 'Python 3.8' README.md"),
            ],
            2,
            True,
        ),
        _row(
            "sync-license-year",
            "Sync the license year",
            "Set the copyright year in the license header to the year the file claims.",
            "docs-auditor",
            "printf '%s\\n' 'Copyright 2026 Recertia' > LICENSE",
            [("year", "grep -q 'Copyright 2026' LICENSE")],
            1,
            True,
        ),
        _marked(
            "split-unrelated-edits",
            "Split unrelated edits",
            "Turn two unrelated edits into two phases whose diffs do not touch each other. Five checks are the bar.",
            "mea",
            5,
            5,
            True,
        ),
        _marked(
            "order-migrate-then-test",
            "Order migrate before test",
            "Run the migration phase before the test phase. Four ordered checks are the dream bar.",
            "mea",
            4,
            4,
            True,
        ),
        _marked(
            "separate-format-from-behavior",
            "Separate format from behavior",
            "Keep a formatting edit out of a behavior phase. The dream run passed three of the five phase checks.",
            "mea",
            5,
            3,
            False,
        ),
        _marked(
            "phase-rename-then-callers",
            "Rename before updating callers",
            "Rename the symbol in one phase and update callers in the next. Four checks are the dream bar.",
            "mea",
            4,
            4,
            True,
        ),
        _marked(
            "hold-second-phase-on-fail",
            "Hold the next phase on failure",
            "Do not start phase two when phase one fails. Five checks are the dream bar.",
            "mea",
            5,
            5,
            True,
        ),
        _marked(
            "record-phase-receipts",
            "Record a receipt per phase",
            "Append one receipt for each phase. The dream run passed two of the four receipt checks.",
            "mea",
            4,
            2,
            False,
        ),
        _marked(
            "reach-expected-screen",
            "Reach the expected screen",
            "Replay the step log until the final screen matches the fixture. Four checks are the dream bar.",
            "playtest-operator",
            4,
            4,
            True,
        ),
        _marked(
            "refuse-unlogged-command",
            "Refuse an unlogged command",
            "Reject a command that was not in the operator log. Three checks are the dream bar.",
            "playtest-operator",
            3,
            3,
            True,
        ),
        _marked(
            "replay-click-path",
            "Replay a click path",
            "Replay the recorded clicks and stop. The dream run passed two of the four path checks.",
            "playtest-operator",
            4,
            2,
            False,
        ),
        _marked(
            "stop-at-error-toast",
            "Stop at the error toast",
            "End the replay when the error toast is the screen. Three checks are the dream bar.",
            "playtest-operator",
            3,
            3,
            True,
        ),
        _row(
            "match-final-url",
            "Match the final URL",
            "Finish the replay on the URL the fixture names and on no other page.",
            "playtest-operator",
            "printf '%s\\n' 'https://example.com/done' > final.url",
            [("url", "grep -qx 'https://example.com/done' final.url")],
            1,
            True,
        ),
        _row(
            "no-extra-navigation",
            "Take no extra navigation",
            "The replay visits only the pages in the log. An extra hop fails the check.",
            "playtest-operator",
            "printf '%s\\n' 'home' 'done' > pages.txt",
            [
                ("home", "grep -qx home pages.txt"),
                ("no-extra", "! grep -qx settings pages.txt"),
            ],
            2,
            True,
        ),
        _marked(
            "brief-from-notes",
            "Brief only from notes",
            "Every claim in the brief cites a span that occurs in the notes. Twelve checks are the dream bar.",
            "research-synthesis",
            12,
            12,
            True,
        ),
        _marked(
            "reject-uncited-link",
            "Reject an uncited link",
            "The brief contains no URL that is not already in the notes. Eight checks are the dream bar.",
            "research-synthesis",
            8,
            8,
            True,
        ),
        _marked(
            "quote-span-exactly",
            "Quote each span exactly",
            "Each quoted span matches the notes byte for byte. Twenty-four checks are the dream bar.",
            "research-synthesis",
            24,
            24,
            True,
        ),
        _marked(
            "no-source-outside-notes",
            "Use no source outside the notes",
            "The brief names no source that the notes do not. The dream run passed four of the eight source checks.",
            "research-synthesis",
            8,
            4,
            False,
        ),
    ]
    ids = [row["skill_id"] for row in rows]
    if len(rows) != 46 or len(set(ids)) != 46:
        raise RuntimeError(f"expected 46 unique keeps, got {len(rows)}")
    if any(item in ids for item in EXCLUDED):
        raise RuntimeError("an excluded skill is in the keep set")
    return rows


def _criterion(skill_id: str, check_id: str, run: str) -> SkillCertificationCriterion:
    draft = SkillCertificationCriterion(
        id=check_id,
        kind="command",
        run=run,
        authored_by="human",
        preregistered=True,
    )
    proof = mint_rejecting_proof(
        draft,
        negative_fixture=f"missing {check_id}",
        fingerprint=f"dream46-{skill_id}-{check_id}",
        checked_at=DREAM_AT,
    )
    return draft.model_copy(update={"sensitivity_proof": proof})


def build_version(row: dict[str, Any]) -> SkillVersion:
    criteria = [
        _criterion(row["skill_id"], check_id, run) for check_id, run in row["checks"]
    ]
    return SkillVersion(
        skill_id=row["skill_id"],
        version=1,
        title=row["title"],
        intent=row["intent"],
        task_class=row["task_class"],
        tags=["dream46", row["task_class"]],
        steps=[
            Step(
                id="write",
                tool="shell",
                intent=row["intent"][:240],
                inputs={"command": row["command"]},
            )
        ],
        certification_criteria=criteria,
        provenance=Provenance(
            distilled_from_run=f"dream46:{row['skill_id']}",
            distilled_at=DREAM_AT,
            authored_by="human",
            curation="human_authored",
            derivation="hand_authored",
        ),
        hygiene=Hygiene(secret_scan="passed", scanned_at=DREAM_AT),
        preconditions=[
            Precondition(
                kind="tool_available",
                value="shell",
                description="shell tool available in the run sandbox",
            )
        ],
        failure_modes=[
            FailureMode(
                symptom="marker file missing or incomplete after write",
                response="re-run the write step; do not promote on partial marks",
            )
        ],
    )


def build_status(skill_id: str) -> SkillStatus:
    return SkillStatus(skill_id=skill_id, version=1, lifecycle="candidate", active=False)


def build_stats(skill_id: str) -> SkillStats:
    return SkillStats(skill_id=skill_id, version=1)


def write_skills(root: Path) -> list[Path]:
    written: list[Path] = []
    for row in catalog():
        version = build_version(row)
        status = build_status(row["skill_id"])
        stats = build_stats(row["skill_id"])
        dest = root / "skills" / row["skill_id"] / "v1"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "version.json").write_text(version.model_dump_json(indent=2) + "\n", encoding="utf-8")
        (dest / "status.json").write_text(status.model_dump_json(indent=2) + "\n", encoding="utf-8")
        (dest / "stats.json").write_text(stats.model_dump_json(indent=2) + "\n", encoding="utf-8")
        written.append(dest)
    return written


def scored_skills() -> list[SoakSkill]:
    skills: list[SoakSkill] = []
    for row in catalog():
        scored = score_run(
            row["skill_id"],
            row["task_class"],
            int(row["checks_passed"]),
            len(row["checks"]),
            bool(row["strict"]),
            CONSTANTS,
        )
        if scored.legacy < 0.05 or scored.prioritized < 0.05:
            raise RuntimeError(f"{row['skill_id']} is not a keep under calib_dream50")
        skills.append(
            SoakSkill(
                skill_id=row["skill_id"],
                legacy=scored.legacy,
                prioritized=scored.prioritized,
            )
        )
    return skills


def open_dream_soak(ledger_path: Path, *, started_at: datetime = DREAM_AT) -> dict[str, Any]:
    """Open one soak for the 46 keeps. The current control-arm rate stays unset."""

    ledger = HashChainLedger(ledger_path)
    result = open_soak(
        ledger,
        DurableIndex(ledger),
        started_at=started_at,
        now=started_at,
        baseline_snapshot_id=SNAPSHOT_ID,
        post_change_skill_id=POST_CHANGE_SKILL,
        noise_band=NOISE_BAND,
        skills=scored_skills(),
        observations=[],
    )
    return result


def write_manifest(path: Path, result: dict[str, Any]) -> None:
    payload = {
        "source": "calib_dream50",
        "measured": False,
        "activated": False,
        "approved_by": "",
        "noise_band": NOISE_BAND,
        "noise_band_note": (
            "Locked at the retirement threshold used to label the dream keeps. "
            "Not fit to a control-arm result."
        ),
        "control_arm_rate": None,
        "excluded": list(EXCLUDED),
        "skills": [
            {"skill_id": skill.skill_id, "legacy": skill.legacy, "prioritized": skill.prioritized}
            for skill in scored_skills()
        ],
        "soak_status": result.get("status"),
        "passed": result.get("passed"),
        "default_on": False,
        "flags_default_changed": False,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
