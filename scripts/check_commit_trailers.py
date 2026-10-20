#!/usr/bin/env python3
"""Allow-list author, committer, and Co-authored-by emails on a commit range.

Pull request: uses the pull-request base and head SHAs (``BASE`` / ``HEAD``),
not GitHub's merge commit.

Push (``BASE`` / ``HEAD`` unset): checks the pushed range
``PUSH_BEFORE..PUSH_AFTER`` (``github.event.before`` / ``github.sha``), so a
direct push to the default branch is never an empty range. When
``PUSH_BEFORE`` is all zeros, empty, or not a commit in this clone (new
branch, first push, or a force push whose old tip was not fetched):

1. if the pushed branch (``PUSH_REF``) is not ``DEFAULT_BRANCH`` and
   ``origin/<DEFAULT_BRANCH>`` exists, the range is
   ``merge-base(origin/<DEFAULT_BRANCH>, PUSH_AFTER)..PUSH_AFTER``;
2. otherwise, the full history of ``PUSH_AFTER``.

On any push, an empty range falls back to checking ``PUSH_AFTER`` alone.

Historical commits outside the allow-list fail this check; they are not
rewritten here.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

_ALLOWED_EXACT = frozenset(
    {
        "cursoragent@" + "cursor.com",
        "robs@" + "gatech.edu",
    }
)
_ALLOWED_SUFFIX = "@users.noreply." + "github.com"
_COAUTHOR = re.compile(r"^Co-authored-by:\s*.*<([^>]+)>\s*$", re.IGNORECASE)


def _allowed(email: str) -> bool:
    value = email.strip().lower()
    if not value:
        return True
    if value in _ALLOWED_EXACT:
        return True
    return value.endswith(_ALLOWED_SUFFIX)


_ZERO_SHA = "0" * 40


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def _is_commit(rev: str) -> bool:
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{rev}^{{commit}}"],
        capture_output=True,
    )
    return probe.returncode == 0


def _push_revs(before: str, after: str) -> list[str]:
    """rev-list arguments for a push; see the module docstring for the fallback."""
    if before and before != _ZERO_SHA and _is_commit(before):
        return [f"{before}..{after}"]
    default = os.environ.get("DEFAULT_BRANCH", "").strip()
    ref = os.environ.get("PUSH_REF", "").strip().removeprefix("refs/heads/")
    if default and ref != default and _is_commit(f"origin/{default}"):
        try:
            return [f"{_git('merge-base', f'origin/{default}', after)}..{after}"]
        except subprocess.CalledProcessError:
            pass
    return [after]


def _range() -> tuple[str, list[str], bool]:
    base = os.environ.get("BASE", "").strip()
    head = os.environ.get("HEAD", "").strip()
    if base and head:
        return head, [f"{base}..{head}"], False
    after = os.environ.get("PUSH_AFTER", "").strip()
    if not after:
        print(
            "Set BASE and HEAD (pull request) or PUSH_BEFORE and PUSH_AFTER (push).",
            file=sys.stderr,
        )
        return "", [], False
    before = os.environ.get("PUSH_BEFORE", "").strip()
    return after, _push_revs(before, after), True


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
    head, revs, push = _range()
    if not revs:
        return 2
    shas = subprocess.check_output(
        ["git", "rev-list", "--reverse", *revs],
        text=True,
    ).split()
    if push and not shas:
        shas = [_git("rev-parse", f"{head}^{{commit}}")]
    span = " ".join(revs)
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
            f"{len(failed)} commit(s) in {span} are outside the email allow-list.",
            file=sys.stderr,
        )
        return 1
    print(f"ok {len(shas)} commit(s) in {span}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
