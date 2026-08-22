#!/usr/bin/env python3
"""Compile docs/architecture2.md from architecture + specification topic files.

The split files under docs/architecture/ and docs/specifications/ remain canonical.
This script concatenates them (plus ADRs, assumptions, and references) into one
downloadable document.

Usage:
    python3 scripts/generate_architecture2.py            # write docs/architecture2.md
    python3 scripts/generate_architecture2.py --check     # exit 1 if committed file drifted
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
OUTPUT_REL = "architecture2.md"
OUTPUT = DOCS / OUTPUT_REL

LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)

# (part title, files relative to docs/). Indexes architecture.md / specifications.md
# are omitted: this compilation's table of contents replaces them.
SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Part I — Architecture",
        (
            "architecture/overview.md",
            "architecture/task-plane.md",
            "architecture/skill-composition.md",
            "architecture/library-lifecycle.md",
            "architecture/portfolio-measurement.md",
            "architecture/improvement-plane.md",
            "architecture/operations.md",
            "architecture/container-sandbox.md",
            "architecture/go-live.md",
            "architecture/openai-compat-gateways.md",
            "architecture/measurement-integrity.md",
            "architecture/risk-and-governance.md",
            "architecture/measurement-and-scope.md",
            "architecture/remaining-work.md",
            "architecture/incident-tabletop.md",
            "architecture/threat-model-deltas.md",
            "architecture/product-console.md",
            "architecture/goal-packs.md",
        ),
    ),
    (
        "Part II — Specifications",
        (
            "specifications/core-entities.md",
            "specifications/graph-execution.md",
            "specifications/retrieval-and-validation.md",
            "specifications/promotion-api-and-observability.md",
            "specifications/product-console.md",
            "specifications/openai-compat-gateways.md",
            "specifications/memory-composition-and-criteria.md",
            "specifications/goal-objects.md",
            "specifications/goal-packs.md",
            "specifications/failure-isolation-and-fanout.md",
            "specifications/evaluation-improvement-and-governance.md",
            "specifications/library-authoring-and-concurrency.md",
            "specifications/registered-workspaces.md",
            "specifications/trajectory-and-replay.md",
        ),
    ),
    (
        "Part III — Architecture decision records",
        (
            "adr/0001-graph-with-loops.md",
            "adr/0002-plural-memory.md",
            "adr/0003-criteria-preregistration.md",
            "adr/0004-offline-improvement-plane.md",
            "adr/0005-self-modification-boundary.md",
            "adr/0006-bounded-library-and-retirement.md",
            "adr/0007-skill-identity-status-and-stats-split.md",
            "adr/0008-optional-join-and-failure-signals.md",
            "adr/0009-contracts-as-code.md",
            "adr/0010-goal-as-primary-input.md",
            "adr/0011-trajectory-and-counterfactual-replay.md",
            "adr/0012-product-console-surfaces.md",
            "adr/0013-openai-compat-gateways.md",
            "adr/0014-goal-packs-as-migration-programs.md",
            "adr/0015-improvement-plane-search.md",
            "adr/0016-interval-bounded-retirement.md",
            "adr/0017-version-write-budget.md",
            "adr/0018-idle-state-offloading.md",
            "adr/0019-external-trajectories.md",
        ),
    ),
    (
        "Part IV — Assumptions and references",
        (
            "assumptions.md",
            "references.md",
        ),
    ),
)

SKIP_LINK_PREFIXES = ("http://", "https://", "mailto:", "#")


def chapter_id(rel: str) -> str:
    return "ch-" + rel.replace("/", "-").removesuffix(".md")


def first_heading(text: str, rel: str) -> str:
    match = HEADING_RE.search(text)
    if match is None:
        return Path(rel).stem.replace("-", " ")
    return match.group(2).strip()


def iter_fenced_segments(text: str) -> list[tuple[bool, str]]:
    """Split markdown into (is_code_fence, segment) pairs."""
    segments: list[tuple[bool, str]] = []
    buf: list[str] = []
    in_fence = False
    for line in text.splitlines(keepends=True):
        if line.startswith("```"):
            if in_fence:
                buf.append(line)
                segments.append((True, "".join(buf)))
                buf = []
                in_fence = False
            else:
                if buf:
                    segments.append((False, "".join(buf)))
                    buf = []
                buf.append(line)
                in_fence = True
        else:
            buf.append(line)
    if buf:
        segments.append((in_fence, "".join(buf)))
    return segments


def rewrite_links(text: str, source_rel: str) -> str:
    source_path = DOCS / source_rel

    def repl(match: re.Match[str]) -> str:
        label, target = match.group(1), match.group(2).strip()
        if target.startswith(SKIP_LINK_PREFIXES):
            return match.group(0)
        href, hash_, frag = target.partition("#")
        if href in ("", "."):
            return match.group(0)
        dest = (source_path.parent / href).resolve()
        new_href = Path(os.path.relpath(dest, DOCS)).as_posix()
        if hash_:
            return f"[{label}]({new_href}#{frag})"
        return f"[{label}]({new_href})"

    out: list[str] = []
    for is_fence, segment in iter_fenced_segments(text):
        if is_fence:
            out.append(segment)
        else:
            out.append(LINK_RE.sub(repl, segment))
    return "".join(out)


def render() -> str:
    included = [rel for _, files in SECTIONS for rel in files]
    missing = [rel for rel in included if not (DOCS / rel).exists()]
    if missing:
        raise FileNotFoundError("missing source files: " + ", ".join(missing))

    bodies: dict[str, str] = {}
    titles: dict[str, str] = {}
    for rel in included:
        raw = (DOCS / rel).read_text(encoding="utf-8")
        titles[rel] = first_heading(raw, rel)
        bodies[rel] = rewrite_links(raw, rel)

    lines: list[str] = [
        "# Recertia architecture2",
        "",
        "> All-in-one compilation of architecture rationale **and** normative",
        "> specifications. Generated by `scripts/generate_architecture2.py`.",
        "> **Do not hand-edit.** Change the topic files and re-run the script.",
        ">",
        "> Canonical split sources remain [`architecture.md`](architecture.md) and",
        "> [`specifications.md`](specifications.md). Structural contracts are",
        "> [`contracts/`](../contracts) per [ADR-0009](adr/0009-contracts-as-code.md).",
        "",
        "## How to read this document",
        "",
        "- **Part I — Architecture** is rationale: why the runtime is a cyclic graph,",
        "  how the three planes stay separate, and how measurement and governance bound",
        "  improvement.",
        "- **Part II — Specifications** is normative: entities, graph contracts, APIs,",
        "  console, remaining-work gates. Where architecture and spec disagree, the spec",
        "  plus [`contracts/`](../contracts) win.",
        "- **Part III — Architecture decision records** records alternatives that were",
        "  considered and rejected.",
        "- **Part IV — Assumptions and references** separates empirical claims still",
        "  under test from the literature the design draws on.",
        "",
        "Each chapter is tagged with its source path so you can find the split file.",
        "Relative links are rewritten to work from `docs/architecture2.md` on GitHub;",
        "the chapter text itself is inlined here so this file is readable offline.",
        "",
        "## Table of contents",
        "",
    ]

    for part_title, files in SECTIONS:
        lines.append(f"### {part_title}")
        lines.append("")
        for rel in files:
            lines.append(f"- [{titles[rel]}](#{chapter_id(rel)}) — `{rel}`")
        lines.append("")

    for part_title, files in SECTIONS:
        lines.extend(["---", "", f"# {part_title}", ""])
        for rel in files:
            lines.extend(
                [
                    f'<a id="{chapter_id(rel)}"></a>',
                    "",
                    f"> Source: [`{rel}`]({rel})",
                    "",
                    bodies[rel].rstrip(),
                    "",
                ]
            )

    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    return text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    generated = render()
    if not args.check:
        OUTPUT.write_text(generated, encoding="utf-8")
        print(f"Wrote {OUTPUT.relative_to(REPO)} ({generated.count(chr(10))} lines)")
        return 0

    existing = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else None
    if generated != existing:
        print("architecture2.md has drifted from the topic files.")
        print("Run `python3 scripts/generate_architecture2.py` and commit the result.")
        return 1
    print("docs/architecture2.md matches the topic files — no drift.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
