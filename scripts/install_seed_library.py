#!/usr/bin/env python3
"""Export M1 seed skills + golden fixtures, then optionally promote them through the gate.

Usage:
  python3 scripts/install_seed_library.py --skills-root skills --golden-root evals/golden
  python3 scripts/install_seed_library.py --promote --runs-root /tmp/recertia-seed
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from recertia.memory.procedural.promote import PromotionError, promote_to_approved  # noqa: E402
from recertia.memory.procedural.seeds import SEED_SKILLS, seed_stats, seed_status_draft  # noqa: E402
from recertia.memory.procedural.store import ImmutabilityError, SkillStore  # noqa: E402
from recertia.retrieval.index import SkillIndex  # noqa: E402

# Golden workspace fixtures and task specs keyed by skill_id.
GOLDEN_FIXTURES: dict[str, dict] = {
    "add-gitignore-entry": {
        "task": {
            "request": "Add *.pyc to the repository .gitignore",
            "expected_skill_id": "add-gitignore-entry",
        },
        "workspace": {".gitignore": "*.egg-info/\n.venv/\n"},
        "expect": {"terminal": "solved"},
    },
    "add-pytest-config": {
        "task": {
            "request": "Add a pytest.ini that sets testpaths to tests",
            "expected_skill_id": "add-pytest-config",
        },
        "workspace": {"pyproject.toml": "[project]\nname = \"demo\"\nversion = \"0.1.0\"\n"},
        "expect": {"terminal": "solved"},
    },
    "add-makefile-target": {
        "task": {
            "request": "Add a Makefile test target that runs pytest",
            "expected_skill_id": "add-makefile-target",
        },
        "workspace": {},
        "expect": {"terminal": "solved"},
    },
    "pin-python-version": {
        "task": {
            "request": "Pin the repository Python version to 3.12",
            "expected_skill_id": "pin-python-version",
        },
        "workspace": {},
        "expect": {"terminal": "solved"},
    },
    "add-editorconfig": {
        "task": {
            "request": "Add a root EditorConfig with Python indent settings",
            "expected_skill_id": "add-editorconfig",
        },
        "workspace": {},
        "expect": {"terminal": "solved"},
    },
    "strip-trailing-whitespace": {
        "task": {
            "request": "Strip trailing whitespace from README.md",
            "expected_skill_id": "strip-trailing-whitespace",
        },
        "workspace": {"README.md": "Hello world   \nSecond line\t\n"},
        "expect": {"terminal": "solved"},
    },
    "add-license-mit": {
        "task": {
            "request": "Add an MIT LICENSE file at the repository root",
            "expected_skill_id": "add-license-mit",
        },
        "workspace": {},
        "expect": {"terminal": "solved"},
    },
    "bump-action-checkout": {
        "task": {
            "request": "Bump actions/checkout to v4 in GitHub workflows",
            "expected_skill_id": "bump-action-checkout",
        },
        "workspace": {
            ".github/workflows/ci.yml": "jobs:\n  build:\n    steps:\n      - uses: actions/checkout@v3\n"
        },
        "expect": {"terminal": "solved"},
    },
    "add-readme-section": {
        "task": {
            "request": "Add an Installation section to the README",
            "expected_skill_id": "add-readme-section",
        },
        "workspace": {"README.md": "# Demo\n\nA demo project.\n"},
        "expect": {"terminal": "solved"},
    },
    "ensure-src-layout": {
        "task": {
            "request": "Ensure a src/demo_pkg package layout exists",
            "expected_skill_id": "ensure-src-layout",
        },
        "workspace": {"pyproject.toml": "[project]\nname = \"demo\"\nversion = \"0.1.0\"\n"},
        "expect": {"terminal": "solved"},
    },
}


def write_goldens(golden_root: Path) -> None:
    for skill_id, fixture in GOLDEN_FIXTURES.items():
        dest = golden_root / "repo-chore" / skill_id
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "task.json").write_text(json.dumps(fixture["task"], indent=2) + "\n")
        (dest / "expect.json").write_text(json.dumps(fixture["expect"], indent=2) + "\n")
        ws = dest / "workspace"
        if ws.exists():
            import shutil

            shutil.rmtree(ws)
        ws.mkdir(parents=True, exist_ok=True)
        for rel, content in fixture["workspace"].items():
            path = ws / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)


def install_versions(store: SkillStore, *, rewrite: bool = False) -> list[str]:
    installed: list[str] = []
    for version in SEED_SKILLS:
        dest = store.version_dir(version.skill_id, version.version) / "version.json"
        if rewrite and dest.exists():
            # Seed bootstrap only: repair hash-bound proofs without bumping versions.
            dest.write_text(version.model_dump_json(indent=2) + "\n", encoding="utf-8")
        else:
            try:
                store.write_version(version)
            except ImmutabilityError:
                pass  # already present — leave the immutable artifact alone
        status_path = store.version_dir(version.skill_id, version.version) / "status.json"
        if not status_path.exists():
            store.write_status(seed_status_draft(version))
        stats_path = store.version_dir(version.skill_id, version.version) / "stats.json"
        if not stats_path.exists():
            store.write_stats(seed_stats(version))
        installed.append(f"{version.skill_id}@v{version.version}")
    return installed


def promote_all(store: SkillStore, golden_root: Path, runs_root: Path, log_dir: Path) -> None:
    for version in SEED_SKILLS:
        status = store.get_status(version.skill_id, version.version)
        if status is not None and status.lifecycle == "approved" and status.certification.golden_set_ref:
            print(f"skip {version.skill_id}@v{version.version} already approved")
            continue
        golden = golden_root / (version.task_class or "repo-chore") / version.skill_id
        try:
            status = promote_to_approved(
                store,
                version.skill_id,
                version.version,
                golden_dir=golden,
                runs_root=runs_root,
                log_dir=log_dir,
                tool_fingerprint={"python": "3.12", "pytest": "8.3.4"},
                repo_root=ROOT,
            )
            print(f"approved {version.skill_id}@v{version.version} active={status.active} "
                  f"golden_ref={status.certification.golden_set_ref}")
        except PromotionError as exc:
            print(f"FAILED {version.skill_id}@v{version.version}: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc


def rebuild_index(store: SkillStore, index_path: Path) -> str:
    index = SkillIndex(index_path)
    try:
        snap = index.rebuild(store.iter_loaded())
    finally:
        index.close()
    return snap


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills-root", type=Path, default=ROOT / "skills")
    parser.add_argument("--golden-root", type=Path, default=ROOT / "evals" / "golden")
    parser.add_argument("--index", type=Path, default=ROOT / ".recertia" / "skill_index.db")
    parser.add_argument("--promote", action="store_true")
    parser.add_argument(
        "--rewrite-versions",
        action="store_true",
        help="Overwrite seed version.json from factories (bootstrap repair only).",
    )
    parser.add_argument("--runs-root", type=Path, default=ROOT / ".recertia" / "seed-runs")
    parser.add_argument("--log-dir", type=Path, default=ROOT / "evals" / "golden" / "_promotion_logs")
    args = parser.parse_args()

    write_goldens(args.golden_root)
    store = SkillStore(args.skills_root)
    installed = install_versions(store, rewrite=args.rewrite_versions)
    print(f"installed {len(installed)} seed skill versions under {args.skills_root}")

    if args.promote:
        promote_all(store, args.golden_root, args.runs_root, args.log_dir)

    snap = rebuild_index(store, args.index)
    print(f"index snapshot={snap} at {args.index}")


if __name__ == "__main__":
    main()
