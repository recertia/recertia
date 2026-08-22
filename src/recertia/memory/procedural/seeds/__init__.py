"""Hand-authored seed skills for M1 plus ADR-0019 computer-use goldens.

Each skill is ``curation: human_authored``, has a passed hygiene stamp, and carries a
hand-authored sensitivity proof per required certification criterion. Golden fixtures live
under ``evals/golden/<task_class>/<skill_id>/`` and are the evidence of the promotion gate.
"""

from __future__ import annotations

from recertia.memory.procedural.seeds.computer_use import (
    docs_vs_shipped,
    playtest_login_path,
    repro_evidence_pack,
)
from recertia.memory.procedural.seeds.factories import (
    SEED_SKILLS,
    add_editorconfig,
    add_gitignore_entry,
    add_license_mit,
    add_makefile_target,
    add_pytest_config,
    add_readme_section,
    bump_action_checkout,
    ensure_src_layout,
    pin_python_version,
    strip_trailing_whitespace,
)
from recertia.memory.procedural.seeds.status import (
    seed_approved_for_tests,
    seed_stats,
    seed_status_draft,
)

__all__ = [
    "SEED_SKILLS",
    "add_editorconfig",
    "add_gitignore_entry",
    "add_license_mit",
    "add_makefile_target",
    "add_pytest_config",
    "add_readme_section",
    "bump_action_checkout",
    "docs_vs_shipped",
    "ensure_src_layout",
    "pin_python_version",
    "playtest_login_path",
    "repro_evidence_pack",
    "seed_approved_for_tests",
    "seed_stats",
    "seed_status_draft",
    "strip_trailing_whitespace",
]
