#!/usr/bin/env python3
"""Allow-list author, committer, and Co-authored-by emails on the PR range.

Uses the pull-request base and head SHAs (``BASE`` / ``HEAD``), not GitHub's
merge commit. Historical commits outside the allow-list fail this check;
they are not rewritten here.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

_ALLOWED_EXACT = frozenset({"cursoragent@" + "cursor.com"})
_ALLOWED_SUFFIX = "@users.noreply." + "github.com"
_COAUTHOR = re.compile(r"^Co-authored-by:\s*.*<([^>]+)>\s*$", re.IGNORECASE)


def _allowed(email: str) -> bool:
    value = email.strip().lower()
    if not value:
        return True
    if value in _ALLOWED_EXACT:
        return True
    return value.endswith(_ALLOWED_SUFFIX)


def _range() -> tuple[str, str]:
    base = os.environ.get("BASE", "").strip()
    head = os.environ.get("HEAD", "").strip()
    if not base or not head:
        print(
            "BASE and HEAD must be the pull-request base and head SHAs.",
            file=sys.stderr,
        )
        return "", ""
    return base, head


def _emails_for(commit: str) -> list[tuple[str, str]]:
    blob = subprocess.check_output(
        [
            "git",
            "log",
            "-1",
            "--format=%ae%n%ce%n%(trailers:key=Co-authored-by)",
            commit,
        ],
        text=True,
    )
    rows: list[tuple[str, str]] = []
    lines = blob.splitlines()
    if lines:
        rows.append(("author", lines[0]))
    if len(lines) > 1:
        rows.append(("committer", lines[1]))
    for line in lines[2:]:
        match = _COAUTHOR.match(line.strip())
        if match:
            rows.append(("Co-authored-by", match.group(1)))
        elif line.strip():
            rows.append(("Co-authored-by", line.strip()))
    return rows


def main() -> int:
    base, head = _range()
    if not base or not head:
        return 2
    shas = subprocess.check_output(
        ["git", "rev-list", "--reverse", f"{base}..{head}"],
        text=True,
    ).split()
    failed: list[str] = []
    for sha in shas:
        bad = [
            f"{kind}={email}"
            for kind, email in _emails_for(sha)
            if not _allowed(email)
        ]
        if bad:
            failed.append(f"{sha} {' '.join(bad)}")
            print(f"FAIL {sha}: {', '.join(bad)}", file=sys.stderr)
    if failed:
        print(
            f"{len(failed)} commit(s) in {base}..{head} are outside the email allow-list.",
            file=sys.stderr,
        )
        return 1
    print(f"ok {len(shas)} commit(s) in {base}..{head}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
