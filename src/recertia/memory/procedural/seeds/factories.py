"""Repo-chore seed skill factories (M1 hand-authored library)."""

from __future__ import annotations

from contracts.skill import Parameter, Precondition, SkillVersion, Step
from recertia.memory.procedural.seeds._common import (
    _HYGIENE,
    _cmd_criterion,
    _prov,
)
from recertia.memory.procedural.seeds.computer_use import (
    docs_vs_shipped,
    playtest_login_path,
    repro_evidence_pack,
)


def add_gitignore_entry() -> SkillVersion:
    return SkillVersion(
        skill_id="add-gitignore-entry",
        version=1,
        title="Add an entry to .gitignore",
        intent=(
            "Append a path or pattern to the repository .gitignore if it is not already present, "
            "without disturbing existing entries."
        ),
        task_class="repo-chore",
        tags=["gitignore", "repo-hygiene"],
        parameters=[Parameter(name="pattern", type="string", required=True)],
        preconditions=[Precondition(kind="file_exists", value=".gitignore")],
        steps=[
            Step(
                id="append",
                tool="shell",
                intent="Append {{pattern}} to .gitignore if missing.",
                # Portable Python (cmd.exe + sh): bash `$pattern` breaks under Windows local-exec.
                inputs={
                    "command": (
                        "python -c \"from pathlib import Path; "
                        "p=Path('.gitignore'); line='*.pyc'; "
                        "text=p.read_text(encoding='utf-8') if p.exists() else ''; "
                        "lines=text.splitlines(); "
                        "p.write_text("
                        "text + ('' if (not text or text.endswith(chr(10))) else chr(10)) "
                        "+ line + chr(10), encoding='utf-8') "
                        "if line not in lines else None\""
                    )
                },
            ),
        ],
        certification_criteria=[
            _cmd_criterion(
                "has-entry",
                (
                    "python -c \"from pathlib import Path; import sys; "
                    "sys.exit(0 if '*.pyc' in Path('.gitignore').read_text("
                    "encoding='utf-8').splitlines() else 1)\""
                ),
                "gitignore without *.pyc",
            ),
        ],
        provenance=_prov("add-gitignore-entry"),
        hygiene=_HYGIENE,
    )


def add_pytest_config() -> SkillVersion:
    return SkillVersion(
        skill_id="add-pytest-config",
        version=1,
        title="Add a minimal pytest configuration",
        intent=(
            "Create or update pytest.ini with a sensible testpaths default so pytest discovers "
            "tests without ad-hoc flags."
        ),
        task_class="repo-chore",
        tags=["pytest", "python", "config"],
        parameters=[],
        preconditions=[Precondition(kind="file_exists", value="pyproject.toml")],
        steps=[
            Step(
                id="write",
                tool="shell",
                intent="Write pytest.ini with testpaths=tests.",
                inputs={
                    "command": "printf '[pytest]\\ntestpaths = tests\\n' > pytest.ini"
                },
            ),
        ],
        certification_criteria=[
            _cmd_criterion("pytest-ini", "test -f pytest.ini", "workspace without pytest.ini"),
            _cmd_criterion(
                "has-testpaths",
                "grep -q 'testpaths' pytest.ini",
                "empty pytest.ini",
            ),
        ],
        provenance=_prov("add-pytest-config"),
        hygiene=_HYGIENE,
    )


def add_makefile_target() -> SkillVersion:
    return SkillVersion(
        skill_id="add-makefile-target",
        version=1,
        title="Add a test target to the Makefile",
        intent=(
            "Ensure the repository Makefile exposes a `test` target that runs the project's "
            "test suite, creating the Makefile if it does not yet exist."
        ),
        task_class="repo-chore",
        tags=["makefile", "tests"],
        parameters=[],
        preconditions=[],
        steps=[
            Step(
                id="ensure",
                tool="shell",
                intent="Create Makefile with a test target if missing.",
                inputs={
                    "command": (
                        "if [ ! -f Makefile ] || ! grep -q '^test:' Makefile; then "
                        "printf 'test:\\n\\tpytest -q\\n' >> Makefile; fi"
                    )
                },
            ),
        ],
        certification_criteria=[
            _cmd_criterion("makefile-exists", "test -f Makefile", "no Makefile"),
            _cmd_criterion("has-test-target", "grep -q '^test:' Makefile", "Makefile without test"),
        ],
        provenance=_prov("add-makefile-target"),
        hygiene=_HYGIENE,
    )


def pin_python_version() -> SkillVersion:
    return SkillVersion(
        skill_id="pin-python-version",
        version=1,
        title="Pin the repository Python version",
        intent=(
            "Write a .python-version file pinning the interpreter so local tooling and CI agree "
            "on the same minor version."
        ),
        task_class="repo-chore",
        tags=["python", "version", "pin"],
        parameters=[
            Parameter(name="version", type="string", required=False, default="3.12"),
        ],
        preconditions=[],
        steps=[
            Step(
                id="write",
                tool="shell",
                intent="Write .python-version with {{version}}.",
                inputs={"command": "echo '3.12' > .python-version"},
            ),
        ],
        certification_criteria=[
            _cmd_criterion("pin-file", "test -f .python-version", "no .python-version"),
            _cmd_criterion("pin-value", "grep -q '3.12' .python-version", "wrong pin"),
        ],
        provenance=_prov("pin-python-version"),
        hygiene=_HYGIENE,
    )


def add_editorconfig() -> SkillVersion:
    return SkillVersion(
        skill_id="add-editorconfig",
        version=1,
        title="Add a root EditorConfig file",
        intent=(
            "Create a root .editorconfig that sets UTF-8, LF endings, and a 4-space indent for "
            "Python so editors agree without per-developer setup."
        ),
        task_class="repo-chore",
        tags=["editorconfig", "style"],
        parameters=[],
        preconditions=[],
        steps=[
            Step(
                id="write",
                tool="shell",
                intent="Write .editorconfig with Python defaults.",
                inputs={
                    "command": (
                        "printf 'root = true\\n\\n[*]\\ncharset = utf-8\\nend_of_line = lf\\n"
                        "\\n[*.py]\\nindent_style = space\\nindent_size = 4\\n' > .editorconfig"
                    )
                },
            ),
        ],
        certification_criteria=[
            _cmd_criterion("ec-exists", "test -f .editorconfig", "no editorconfig"),
            _cmd_criterion("ec-python", "grep -q '\\[\\*\\.py\\]' .editorconfig", "no python section"),
        ],
        provenance=_prov("add-editorconfig"),
        hygiene=_HYGIENE,
    )


def strip_trailing_whitespace() -> SkillVersion:
    return SkillVersion(
        skill_id="strip-trailing-whitespace",
        version=1,
        title="Strip trailing whitespace from text files",
        intent=(
            "Remove trailing whitespace from tracked text files in the working tree so diffs "
            "stay focused on substantive changes."
        ),
        task_class="repo-chore",
        tags=["whitespace", "hygiene"],
        parameters=[Parameter(name="path", type="path", required=False, default=".")],
        preconditions=[Precondition(kind="file_exists", value="README.md")],
        steps=[
            Step(
                id="strip",
                tool="shell",
                intent="Strip trailing whitespace from README.md.",
                inputs={
                    "command": (
                        "sed -i 's/[[:space:]]*$//' README.md"
                    )
                },
            ),
        ],
        certification_criteria=[
            _cmd_criterion(
                "no-trailing",
                "! grep -q '[[:space:]]$' README.md",
                "README with trailing spaces",
            ),
        ],
        provenance=_prov("strip-trailing-whitespace"),
        hygiene=_HYGIENE,
    )


def add_license_mit() -> SkillVersion:
    return SkillVersion(
        skill_id="add-license-mit",
        version=1,
        title="Add an MIT LICENSE file",
        intent=(
            "Write a standard MIT LICENSE file at the repository root when one is missing, so "
            "the project has an explicit open-source grant."
        ),
        task_class="repo-chore",
        tags=["license", "legal"],
        parameters=[],
        preconditions=[],
        steps=[
            Step(
                id="write",
                tool="shell",
                intent="Write MIT LICENSE.",
                inputs={
                    "command": (
                        "printf 'MIT License\\n\\nCopyright (c) 2026\\n\\nPermission is hereby "
                        "granted, free of charge, to any person obtaining a copy of this software "
                        "and associated documentation files.\\n' > LICENSE"
                    )
                },
            ),
        ],
        certification_criteria=[
            _cmd_criterion("license-exists", "test -f LICENSE", "no LICENSE"),
            _cmd_criterion("license-mit", "grep -q 'MIT License' LICENSE", "non-MIT LICENSE"),
        ],
        provenance=_prov("add-license-mit"),
        hygiene=_HYGIENE,
    )


def bump_action_checkout() -> SkillVersion:
    return SkillVersion(
        skill_id="bump-action-checkout",
        version=1,
        title="Bump actions/checkout in a workflow file",
        intent=(
            "Raise the actions/checkout pin in .github/workflows to a target major version so "
            "CI stays on a maintained action release."
        ),
        task_class="repo-chore",
        tags=["github-actions", "ci"],
        parameters=[Parameter(name="major", type="string", required=False, default="4")],
        preconditions=[Precondition(kind="path_glob", value=".github/workflows/*.yml")],
        steps=[
            Step(
                id="bump",
                tool="shell",
                intent="Rewrite actions/checkout@vN to @v4.",
                inputs={
                    "command": (
                        "for f in .github/workflows/*.yml; do "
                        "sed -i 's|actions/checkout@v[0-9]*|actions/checkout@v4|g' \"$f\"; done"
                    )
                },
            ),
        ],
        certification_criteria=[
            _cmd_criterion(
                "checkout-v4",
                "grep -q 'actions/checkout@v4' .github/workflows/*.yml",
                "workflow still on checkout@v3",
            ),
        ],
        provenance=_prov("bump-action-checkout"),
        hygiene=_HYGIENE,
    )


def add_readme_section() -> SkillVersion:
    return SkillVersion(
        skill_id="add-readme-section",
        version=1,
        title="Add an Installation section to the README",
        intent=(
            "Ensure README.md contains an Installation heading with a minimal install command "
            "so newcomers can get a working environment quickly."
        ),
        task_class="repo-chore",
        tags=["readme", "docs"],
        parameters=[],
        preconditions=[Precondition(kind="file_exists", value="README.md")],
        steps=[
            Step(
                id="append",
                tool="shell",
                intent="Append Installation section if missing.",
                inputs={
                    "command": (
                        "grep -q '^## Installation' README.md || "
                        "printf '\\n## Installation\\n\\n```\\npip install -e .\\n```\\n' >> README.md"
                    )
                },
            ),
        ],
        certification_criteria=[
            _cmd_criterion(
                "has-install",
                "grep -q '^## Installation' README.md",
                "README without Installation",
            ),
        ],
        provenance=_prov("add-readme-section"),
        hygiene=_HYGIENE,
    )


def ensure_src_layout() -> SkillVersion:
    return SkillVersion(
        skill_id="ensure-src-layout",
        version=1,
        title="Ensure a src/ package layout exists",
        intent=(
            "Create a src/<package>/__init__.py skeleton when the repository still uses a flat "
            "layout, so packaging and imports share one convention."
        ),
        task_class="repo-chore",
        tags=["python", "layout", "packaging"],
        parameters=[Parameter(name="package", type="string", required=True)],
        preconditions=[Precondition(kind="file_exists", value="pyproject.toml")],
        steps=[
            Step(
                id="mkdir",
                tool="shell",
                intent="Create src/demo_pkg/__init__.py.",
                inputs={
                    "command": (
                        "mkdir -p src/demo_pkg && "
                        "printf '\"\"\"demo package\"\"\"\\n' > src/demo_pkg/__init__.py"
                    )
                },
            ),
        ],
        certification_criteria=[
            _cmd_criterion("init-exists", "test -f src/demo_pkg/__init__.py", "no src package"),
        ],
        provenance=_prov("ensure-src-layout"),
        hygiene=_HYGIENE,
    )


SEED_SKILLS: list[SkillVersion] = [
    add_gitignore_entry(),
    add_pytest_config(),
    add_makefile_target(),
    pin_python_version(),
    add_editorconfig(),
    strip_trailing_whitespace(),
    add_license_mit(),
    bump_action_checkout(),
    add_readme_section(),
    ensure_src_layout(),
    repro_evidence_pack(),
    playtest_login_path(),
    docs_vs_shipped(),
]

