#!/usr/bin/env python3
"""Flag raw file writes outside ``recertia.fsutil``.

``--check`` fails when the per-file counts differ from
``scripts/raw_writes_baseline.txt``. Existing sites are grandfathered.
A new ``write_text`` / ``write_bytes`` / write-mode ``open`` increases a
count and fails. Migrating a site requires shrinking the baseline.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "recertia"
BASELINE = Path(__file__).resolve().parent / "raw_writes_baseline.txt"
ALLOW_FILES = {
    "solver/handlers.py",
    "nodes/fan_out.py",
    "nodes/join.py",
}


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _mode_from_call(node: ast.Call, *, index: int) -> str | None:
    if len(node.args) > index:
        found = _const_str(node.args[index])
        if found is not None:
            return found
    for keyword in node.keywords:
        if keyword.arg == "mode":
            return _const_str(keyword.value)
    return None


def _is_write_mode(mode: str) -> bool:
    head = mode.split("+", 1)[0]
    return any(char in head for char in "wax")


def _hits(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in {"write_text", "write_bytes"}:
            count += 1
            continue
        if isinstance(func, ast.Attribute) and func.attr == "open":
            mode = _mode_from_call(node, index=0)
            if mode is not None and _is_write_mode(mode):
                count += 1
            continue
        if isinstance(func, ast.Name) and func.id == "open":
            mode = _mode_from_call(node, index=1)
            if mode is not None and _is_write_mode(mode):
                count += 1
    return count


def inventory() -> Counter[str]:
    counts: Counter[str] = Counter()
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith("fsutil/") or rel in ALLOW_FILES:
            continue
        found = _hits(path)
        if found:
            counts[rel] = found
    return counts


def _format(counts: Counter[str]) -> str:
    lines = [f"{rel}\t{counts[rel]}" for rel in sorted(counts)]
    return ("\n".join(lines) + "\n") if lines else ""


def _parse(text: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        rel, _, raw = stripped.partition("\t")
        counts[rel] = int(raw)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="compare to the baseline")
    parser.add_argument("--write-baseline", action="store_true", help="overwrite the baseline")
    args = parser.parse_args(argv)
    counts = inventory()
    rendered = _format(counts)
    if args.write_baseline:
        BASELINE.write_text(rendered, encoding="utf-8")
        print(f"wrote {BASELINE} ({len(counts)} files)")
        return 0
    if not args.check:
        sys.stdout.write(rendered)
        return 0
    expected = _parse(BASELINE.read_text(encoding="utf-8")) if BASELINE.is_file() else Counter()
    if counts == expected:
        print(f"raw writes match baseline ({sum(counts.values())} sites)")
        return 0
    print("raw write inventory drifted from scripts/raw_writes_baseline.txt", file=sys.stderr)
    for rel in sorted(set(counts) | set(expected)):
        if counts[rel] != expected[rel]:
            print(f"  {rel}: baseline={expected[rel]} actual={counts[rel]}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
