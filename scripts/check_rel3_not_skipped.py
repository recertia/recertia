#!/usr/bin/env python3
"""Fail if a REL-3 probe or regression test is marked skip/xfail.

Runtime skips are also refused by the pytest session hook in tests/conftest.py.
This script is the CI static half: an unconditional skip or xfail on those
files fails the job even if the suite is not run.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REL3_FILES = (
    ROOT / "tests/unit/graph/test_rel3_probe.py",
    ROOT / "tests/unit/graph/test_fenced_persistence.py",
    ROOT / "tests/unit/persistence/test_import_legacy.py",
)


def _mark_name(node: ast.AST) -> str | None:
    cur: ast.AST = node
    parts: list[str] = []
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


def _is_skip_or_xfail(name: str) -> bool:
    return name in {
        "skip",
        "xfail",
        "pytest.mark.skip",
        "pytest.mark.xfail",
        "mark.skip",
        "mark.xfail",
    }


def _decorator_marks(deco: ast.AST) -> str | None:
    target = deco
    if isinstance(deco, ast.Call):
        target = deco.func
    return _mark_name(target)


def main() -> int:
    failed: list[str] = []
    for path in REL3_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            for deco in node.decorator_list:
                mark = _decorator_marks(deco)
                if mark is None or not _is_skip_or_xfail(mark):
                    continue
                failed.append(f"{rel}::{node.name} marked {mark}")
    if failed:
        print("REL-3 skip/xfail guard failed:", file=sys.stderr)
        for line in failed:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"ok {len(REL3_FILES)} REL-3 files have no skip/xfail marks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
