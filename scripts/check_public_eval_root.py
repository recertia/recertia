#!/usr/bin/env python3
"""Refuse to point weekly-ops (or any public job) at a non-public eval root (REC-022).

Each path given must:
- be relative, with no ``..``, and sit under one of ``PUBLIC_ROOTS``;
- never touch ``evals/heldout`` or any other private location;
- resolve (symlinks followed) inside the same public root of this checkout;
- contain only git-tracked files, so a dropped-in or decrypted private fixture fails;
- match the index, so a decrypted file written over a tracked path fails too.

It also fails if any ``HELDOUT_*`` variable is set in the environment.

Output names the failing argument index and a fixed reason, never a path.

Usage: python3 scripts/check_public_eval_root.py evals/golden evals/probes/repo-chore.json ...
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parent.parent
PUBLIC_ROOTS = ("evals/golden", "evals/probes", "evals/canary")
PRIVATE_PARTS = ("heldout",)


def _tracked(repo: Path, rel: str) -> set[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z", "--", rel],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {p for p in out.split("\0") if p}


def _modified(repo: Path, rel: str) -> bool:
    """True if any tracked file under ``rel`` differs from the index (content, mode or type)."""

    proc = subprocess.run(
        ["git", "-C", str(repo), "diff", "--quiet", "--no-ext-diff", "--", rel],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode not in (0, 1):
        raise subprocess.CalledProcessError(proc.returncode, proc.args)
    return proc.returncode == 1


def check_path(raw: str, repo: Path = REPO) -> str | None:
    """Return a fixed failure reason, or ``None`` if ``raw`` is a public eval path."""

    if not raw or "\\" in raw or "\x00" in raw:
        return "malformed"
    rel = PurePosixPath(raw)
    if rel.is_absolute():
        return "absolute_path"
    if any(part in ("..", "") for part in rel.parts):
        return "parent_traversal"
    if any(private in part.lower() for part in rel.parts for private in PRIVATE_PARTS):
        return "private_root"
    root = next((r for r in PUBLIC_ROOTS if rel == PurePosixPath(r) or rel.is_relative_to(r)), None)
    if root is None:
        return "not_public_root"
    target = repo / rel
    if not target.exists():
        return "missing"
    if not target.resolve().is_relative_to((repo / root).resolve()):
        return "escapes_public_root"
    files = [target] if target.is_file() else [p for p in target.rglob("*") if p.is_file() or p.is_symlink()]
    for path in files:
        if path.is_symlink() and not path.resolve().is_relative_to((repo / root).resolve()):
            return "escapes_public_root"
    tracked = _tracked(repo, rel.as_posix())
    if not tracked:
        return "untracked"
    if any(p.relative_to(repo).as_posix() not in tracked for p in files):
        return "untracked"
    if _modified(repo, rel.as_posix()):
        return "modified"
    return None


def check(paths: Sequence[str], env: Mapping[str, str], repo: Path = REPO) -> list[str]:
    errors = (
        [f"heldout_env_set count={sum(1 for k in env if k.startswith('HELDOUT_'))}"]
        if any(k.startswith("HELDOUT_") for k in env)
        else []
    )
    if not paths:
        errors.append("no_paths")
    for index, raw in enumerate(paths):
        reason = check_path(raw, repo)
        if reason:
            errors.append(f"arg={index} reason={reason}")
    return errors


def main(argv: Sequence[str] | None = None) -> int:
    paths = list(sys.argv[1:] if argv is None else argv)
    errors = check(paths, os.environ)
    if errors:
        for line in errors:
            print(f"public-eval-root: FAIL {line}")
        return 1
    print(f"public-eval-root: PASS paths={len(paths)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
