#!/usr/bin/env python3
"""Allow-list author, committer, and Co-authored-by emails on a commit range.

Uses ``BASE`` / ``HEAD`` (the pull-request base and head SHAs when a PR
exists, not GitHub's merge commit). On push, when those are unset, the
range is ``git merge-base origin/main HEAD`` .. ``HEAD``. Every commit in
``BASE..HEAD`` is checked. The allow-list is the Cursor agent address and
GitHub noreply suffixes only (built at runtime so this file does not
contain a store-time email literal).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

_ALLOWED_EXACT = frozenset({"cursoragent@" + "cursor.com"})
_ALLOWED_SUFFIX = "@users.noreply." + "github.com"
_COAUTHOR = re.compile(r"^Co-authored-by:\s*.*<([^>]+)>\s*$", re.IGNORECASE)
_EMAIL = re.compile(r"<([^>]+)>")


def _allowed(email: str) -> bool:
    value = email.strip().lower()
    if not value:
        return True
    if value in _ALLOWED_EXACT:
        return True
    return value.endswith(_ALLOWED_SUFFIX)


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def _range() -> tuple[str, str]:
    base = os.environ.get("BASE", "").strip()
    head = os.environ.get("HEAD", "").strip() or "HEAD"
    if not base:
        try:
            base = _git("merge-base", "origin/main", "HEAD")
        except subprocess.CalledProcessError:
            print(
                "BASE is unset and git merge-base origin/main HEAD failed.",
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
        stripped = line.strip()
        if not stripped:
            continue
        match = _COAUTHOR.match(stripped)
        if match:
            rows.append(("Co-authored-by", match.group(1)))
            continue
        angle = _EMAIL.search(stripped)
        if angle:
            rows.append(("Co-authored-by", angle.group(1)))
        else:
            rows.append(("Co-authored-by", stripped))
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
