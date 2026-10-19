#!/usr/bin/env python3
"""Fail if a REL-3 probe or regression test is skipped, xfailed, or marked so.

Covers tests/probes/rel3_*, the probe wrappers, test_import_legacy.py, and
test_fenced_persistence.py. Runtime skips are also refused by the pytest
session hook in tests/conftest.py. This script is the CI half: static marks
fail even if the suite is not run, and a dedicated pytest pass fails if any
of those tests skip or xfail at runtime.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REL3_TEST_FILES = (
    ROOT / "tests/unit/graph/test_rel3_probe.py",
    ROOT / "tests/unit/graph/test_fenced_persistence.py",
    ROOT / "tests/unit/persistence/test_import_legacy.py",
)


def _rel3_files() -> list[Path]:
    files = list(REL3_TEST_FILES)
    files.extend(sorted((ROOT / "tests/probes").glob("rel3_*")))
    return files


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


def _static_check() -> list[str]:
    failed: list[str] = []
    for path in _rel3_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if path.parent.name == "probes" or node.name.startswith("test_"):
                for deco in node.decorator_list:
                    mark = _decorator_marks(deco)
                    if mark is None or not _is_skip_or_xfail(mark):
                        continue
                    failed.append(f"{rel}::{node.name} marked {mark}")
    return failed


def _runtime_check() -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "rel3-skip-guard.xml"
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            *[str(p.relative_to(ROOT)) for p in REL3_TEST_FILES],
            "--override-ini=xfail_strict=true",
            f"--junitxml={report}",
            "-q",
            "--tb=no",
            "-p",
            "no:cacheprovider",
        ]
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        failed: list[str] = []
        if not report.is_file():
            failed.append(
                f"pytest produced no junit report (exit {proc.returncode}): {proc.stderr[-400:]}"
            )
            return failed
        tree = ET.parse(report)
        for case in tree.iter("testcase"):
            file_name = case.get("file") or case.get("classname") or ""
            nodeid = f"{file_name}::{case.get('name')}"
            skipped = case.find("skipped")
            if skipped is None:
                continue
            kind = "xfail" if "xfail" in (skipped.get("type") or "").lower() else "skipped"
            failed.append(f"{kind}: {nodeid}")
        if proc.returncode not in (0, 1) and not failed:
            failed.append(f"pytest failed (exit {proc.returncode}): {proc.stderr[-400:]}")
        return failed


def main() -> int:
    static_failed = _static_check()
    if static_failed:
        print("REL-3 skip/xfail guard failed (static):", file=sys.stderr)
        for line in static_failed:
            print(f"  {line}", file=sys.stderr)
        return 1
    runtime_failed = _runtime_check()
    if runtime_failed:
        print("REL-3 skip/xfail guard failed (runtime):", file=sys.stderr)
        for line in runtime_failed:
            print(f"  {line}", file=sys.stderr)
        return 1
    n = len(_rel3_files())
    print(f"ok {n} REL-3 files have no skip/xfail marks and no skipped/xfailed tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
