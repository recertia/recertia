"""GitHub heading-id contract for scripts/check_cross_refs.py (bug 4).

GitHub's rendered HTML (contents API, Accept: application/vnd.github.html) uses
``user-content-`` prefixed ids whose suffix is: lowercase, punctuation dropped
(em dash included), underscores kept, each whitespace character turned into ``-``.
Spaces around a dropped em dash therefore become ``--``. The checker must not
accept a prefix/suffix fallback against a real heading.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.check_cross_refs import _slugify, check  # noqa: E402

# Captured from GitHub HTML ids for recertia/recertia@bc2a0ee (prefix stripped).
GITHUB_REFERENCES_IDS = {
    "19-trajectory-events-are-the-missing-measurement-substrate--without-weight-updates",
    "110-strategy-lock-in-is-already-encoded--confirming-not-changing",
    "111-phantom-gains-every-transition-statistic-needs-a-measured-null--confirming-not-changing",
    "112-skill-set-packing-under-a-token-budget-is-retrieve-time-not-a-new-node--not-implemented",
    "14-recuris-2026-08-26--cite-do-not-reshape-t3",
    "151-falsifiable-release-gates-score-9--confirm",
    "152-assay-score-9--confirm",
    "153-pace-score-9--confirm",
    "154-self-authored-verification-score-9--confirm",
}
GITHUB_A10 = (
    "a10-checked-working-memory-plus-component-localized-patches-lift-versus-m_0-on-repo-chore"
)


def test_each_space_becomes_a_hyphen() -> None:
    assert _slugify("Hello  world") == "hello--world"
    assert _slugify("One two three") == "one-two-three"


def test_em_dash_is_dropped_leaving_surrounding_hyphens() -> None:
    assert (
        _slugify("1.9 Trajectory events are the missing measurement substrate — without weight updates")
        == "19-trajectory-events-are-the-missing-measurement-substrate--without-weight-updates"
    )


def test_underscore_is_kept() -> None:
    assert (
        _slugify(
            "a10. Checked working memory plus component-localized patches lift versus $M_0$ on repo-chore"
        )
        == GITHUB_A10
    )


def test_slugify_matches_captured_github_ids() -> None:
    headings = {
        "1.9 Trajectory events are the missing measurement substrate — without weight updates",
        "1.10 Strategy lock-in is already encoded — confirming, not changing",
        "1.11 Phantom Gains: every transition statistic needs a measured null — confirming, not changing",
        "1.12 Skill-set packing under a token budget is retrieve-time, not a new node — not implemented",
        "14. Recuris (2026-08-26) — cite; do not reshape T3",
        "15.1 Falsifiable Release Gates (score 9) — confirm",
        "15.2 ASSAY (score 9) — confirm",
        "15.3 PACE (score 9) — confirm",
        "15.4 Self-authored verification (score 9) — confirm",
    }
    assert {_slugify(h) for h in headings} == GITHUB_REFERENCES_IDS


def test_prefix_fallback_is_rejected(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text(
        "# Hello world extra\n\nSee [truncated](#hello-world).\n",
        encoding="utf-8",
    )
    errors = check(docs)
    assert any("dangling fragment #hello-world" in error for error in errors)


def test_double_hyphen_anchor_is_accepted(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text(
        "# Miner — cold-start\n\nSee [here](#miner--cold-start).\n",
        encoding="utf-8",
    )
    assert check(docs) == []
