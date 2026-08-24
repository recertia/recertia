#!/usr/bin/env python3
"""R3: fail on dangling markdown links and § targets in docs/ (refactor-plan R3)."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"

LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)
SPLIT_DOCS: dict[str, tuple[str, ...]] = {
    "architecture.md": (
        "architecture/overview.md",
        "architecture/task-plane.md",
        "architecture/skill-composition.md",
        "architecture/library-lifecycle.md",
        "architecture/improvement-plane.md",
        "architecture/arxiv-ingest.md",
        "architecture/operations.md",
        "architecture/measurement-integrity.md",
        "architecture/risk-and-governance.md",
        "architecture/measurement-and-scope.md",
        "architecture/container-sandbox.md",
        "architecture/go-live.md",
        "architecture/openai-compat-gateways.md",
        "architecture/remaining-work.md",
        "architecture/incident-tabletop.md",
        "architecture/threat-model-deltas.md",
        "architecture/product-console.md",
    ),
    "specifications.md": (
        "specifications/core-entities.md",
        "specifications/graph-execution.md",
        "specifications/retrieval-and-validation.md",
        "specifications/promotion-api-and-observability.md",
        "specifications/product-console.md",
        "specifications/openai-compat-gateways.md",
        "specifications/memory-composition-and-criteria.md",
        "specifications/failure-isolation-and-fanout.md",
        "specifications/evaluation-improvement-and-governance.md",
        "specifications/library-authoring-and-concurrency.md",
    ),
}


def _slugify(heading: str) -> str:
    text = heading.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text.strip("-")


def _anchors_in(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    anchors = {_slugify(m.group(2)) for m in HEADING_RE.finditer(text)}
    anchors.update(re.findall(r'id="([^"]+)"', text))
    return anchors


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def check(docs_root: Path = DOCS) -> list[str]:
    errors: list[str] = []
    docs_root = docs_root.resolve()
    base = docs_root.parent  # usually the repo root; tmp_path in tests
    # architecture2.md is a generated compilation; topic files remain canonical.
    md_files = sorted(
        p for p in docs_root.rglob("*.md")
        if "archive" not in p.parts and p.name != "architecture2.md"
    )
    anchor_index: dict[Path, set[str]] = {p: _anchors_in(p) for p in md_files}

    for path in md_files:
        text = path.read_text(encoding="utf-8")
        for match in LINK_RE.finditer(text):
            target = match.group(2).strip()
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            href, _, frag = target.partition("#")
            if href in ("", "."):
                dest = path
            else:
                dest = (path.parent / href).resolve()
                if not dest.exists():
                    errors.append(
                        f"{_rel(path, base)}: dangling path {href!r} in {target}"
                    )
                    continue
            if frag:
                anchors = anchor_index.get(dest)
                if anchors is None and dest.exists() and dest.suffix == ".md":
                    anchors = _anchors_in(dest)
                    anchor_index[dest] = anchors
                if anchors is None:
                    errors.append(
                        f"{_rel(path, base)}: dangling fragment #{frag} in {target}"
                    )
                elif frag not in anchors and not any(
                    a.startswith(frag) or frag.startswith(a) for a in anchors
                ):
                    errors.append(
                        f"{_rel(path, base)}: dangling fragment #{frag} in {target} "
                        f"(dest={_rel(dest, base)})"
                    )

    for index_name, topics in SPLIT_DOCS.items():
        index = docs_root / index_name
        if not index.exists():
            continue
        text = index.read_text(encoding="utf-8")
        for topic in topics:
            if not (docs_root / topic).exists():
                errors.append(f"docs/{topic}: missing split-document topic")
            elif f"]({topic})" not in text:
                errors.append(
                    f"docs/{index_name}: missing index link to {topic}"
                )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", default=True)
    _ = parser.parse_args()
    errors = check()
    if errors:
        print("cross-ref check failed:", file=sys.stderr)
        for err in errors[:50]:
            print(f"  {err}", file=sys.stderr)
        if len(errors) > 50:
            print(f"  ... and {len(errors) - 50} more", file=sys.stderr)
        return 1
    print("cross-ref check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
