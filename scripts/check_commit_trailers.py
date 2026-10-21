#!/usr/bin/env python3
"""Allow-list author, committer, and identity-trailer emails on a commit range.

Checked on every commit in the range: author, committer, and every git
trailer whose key ends in ``-by`` (case-insensitive; e.g. Co-authored-by,
Signed-off-by, Reviewed-by, Acked-by, Tested-by, Reported-by). An empty or
missing email fails.

Pull request: uses the pull-request base and head SHAs (``BASE`` / ``HEAD``),
not GitHub's merge commit.

Push (``BASE`` / ``HEAD`` unset): checks the pushed range
``PUSH_BEFORE..PUSH_AFTER`` (``github.event.before`` / ``github.sha``), so a
direct push to the default branch is never an empty range. CI fetches
``PUSH_BEFORE`` first so a force push's old tip is usually present.

* ``PUSH_BEFORE`` non-zero but not a commit in this clone (force push whose
  old tip could not be fetched): on ``DEFAULT_BRANCH`` this FAILS CLOSED
  (exit 1); checking only the tip would let earlier commits through. On any
  other branch, the merge-base rule below applies.
* ``PUSH_BEFORE`` all zeros or empty (branch creation): on any branch other
  than ``DEFAULT_BRANCH``, the range is
  ``merge-base(origin/<DEFAULT_BRANCH>, PUSH_AFTER)..PUSH_AFTER``. Creating
  ``DEFAULT_BRANCH`` itself FAILS CLOSED: its pre-policy history is outside
  the allow-list and is not grandfathered, and checking only the tip would
  pass unchecked commits. That one run needs a maintainer to review it.
* No ``origin/<DEFAULT_BRANCH>`` or no merge-base: FAILS CLOSED.

A push whose range is empty (for example a rewind, or a new branch created
at a commit already on the default branch) checks the ``PUSH_AFTER`` commit
alone.

GitHub's web-flow address is accepted as committer only (merge button,
squash merges, and "Update branch"), never as author or in a trailer.

Output is redacted because CI logs are public: a failing value is reported
only as ``<sha>: <role>=non-allowlisted`` (or ``<role>=<empty>``), never the
address, its parts, or a name. git's own stderr is captured and discarded,
and unexpected errors print only their type.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

_ALLOWED_EXACT = frozenset(
    {
        "cursoragent@" + "cursor.com",
    }
)
_ALLOWED_SUFFIX = "@users.noreply." + "github.com"
_WEB_FLOW_COMMITTER = "noreply@" + "github.com"
_IDENTITY_TRAILER = re.compile(r"^([A-Za-z0-9-]*-by)\s*:\s*(.*)$", re.IGNORECASE)
_ANGLE_EMAIL = re.compile(r"<([^>]*)>")


def _allowed(email: str, kind: str = "author") -> bool:
    value = email.strip().lower()
    if not value:
        return False
    if value in _ALLOWED_EXACT:
        return True
    if kind == "committer" and value == _WEB_FLOW_COMMITTER:
        return True
    return value.endswith(_ALLOWED_SUFFIX)


_ZERO_SHA = "0" * 40


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True, stderr=subprocess.PIPE).strip()


def _is_commit(rev: str) -> bool:
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{rev}^{{commit}}"],
        capture_output=True,
    )
    return probe.returncode == 0


class RangeError(Exception):
    """The pushed range cannot be determined; the check fails closed."""


def _push_revs(before: str, after: str) -> tuple[list[str], str]:
    """rev-list arguments and a label for a push; see the module docstring."""
    zero = not before or before == _ZERO_SHA
    if not zero and _is_commit(before):
        return [f"{before}..{after}"], f"{before}..{after}"
    default = os.environ.get("DEFAULT_BRANCH", "").strip()
    ref = os.environ.get("PUSH_REF", "").strip().removeprefix("refs/heads/")
    if not default or ref == default:
        if zero:
            raise RangeError(
                f"push creates the default branch at {after[:12]}; its history is"
                " not grandfathered, so the range cannot be checked. Failing"
                " closed: a maintainer must review this push."
            )
        raise RangeError(
            f"push before={before[:12]} is not in this clone (force push whose"
            " old tip could not be fetched); the pushed range cannot be"
            " computed. Failing closed instead of checking only the tip."
        )
    if not _is_commit(f"origin/{default}"):
        raise RangeError(f"origin/{default} is not in this clone; failing closed.")
    try:
        base = _git("merge-base", f"origin/{default}", after)
    except subprocess.CalledProcessError as exc:
        raise RangeError(
            f"{after[:12]} shares no history with origin/{default}; failing closed."
        ) from exc
    return [f"{base}..{after}"], f"{base}..{after}"


def _range() -> tuple[str, list[str], str, bool]:
    base = os.environ.get("BASE", "").strip()
    head = os.environ.get("HEAD", "").strip()
    if base and head:
        return head, [f"{base}..{head}"], f"{base}..{head}", False
    after = os.environ.get("PUSH_AFTER", "").strip()
    if not after:
        print(
            "Set BASE and HEAD (pull request) or PUSH_BEFORE and PUSH_AFTER (push).",
            file=sys.stderr,
        )
        return "", [], "", False
    before = os.environ.get("PUSH_BEFORE", "").strip()
    revs, label = _push_revs(before, after)
    return after, revs, label, True


def _emails_for(commit: str) -> list[tuple[str, str]]:
    blob = subprocess.check_output(
        [
            "git",
            "log",
            "-1",
            "--format=%ae%n%ce%n%(trailers:only,unfold)",
            commit,
        ],
        text=True,
        stderr=subprocess.PIPE,
    )
    lines = blob.splitlines()
    rows: list[tuple[str, str]] = [
        ("author", lines[0] if lines else ""),
        ("committer", lines[1] if len(lines) > 1 else ""),
    ]
    for line in lines[2:]:
        match = _IDENTITY_TRAILER.match(line.strip())
        if not match:
            continue
        value = match.group(2).strip()
        email = _ANGLE_EMAIL.search(value)
        rows.append((match.group(1), email.group(1) if email else value))
    return rows


def _redacted(kind: str, email: str) -> str:
    """Role and verdict only; the value itself is never printed."""
    return f"{kind}={'<empty>' if not email.strip() else 'non-allowlisted'}"


def _main() -> int:
    try:
        head, revs, label, push = _range()
    except RangeError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    if not revs:
        return 2
    shas = subprocess.check_output(
        ["git", "rev-list", "--reverse", *revs],
        text=True,
        stderr=subprocess.PIPE,
    ).split()
    if push and not shas:
        shas = [_git("rev-parse", f"{head}^{{commit}}")]
        label = f"commit {shas[0][:12]} (tip only, empty push range)"
    failed: list[str] = []
    for sha in shas:
        bad = [
            _redacted(kind, email)
            for kind, email in _emails_for(sha)
            if not _allowed(email, kind)
        ]
        if bad:
            failed.append(f"{sha} {' '.join(bad)}")
            print(f"FAIL {sha}: {', '.join(bad)}", file=sys.stderr)
    if failed:
        print(
            f"{len(failed)} commit(s) in {label} are outside the email allow-list.",
            file=sys.stderr,
        )
        return 1
    print(f"ok {len(shas)} commit(s) in {label}")
    return 0


def main() -> int:
    # Nothing from git output or exception text is echoed: either could carry
    # an address or a name into a public log. Exit code 1 as for an uncaught
    # exception before.
    try:
        return _main()
    except subprocess.CalledProcessError as exc:
        verb = exc.cmd[1] if isinstance(exc.cmd, list) and len(exc.cmd) > 1 else "git"
        print(f"FAIL git {verb} exited {exc.returncode} (git output suppressed)", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"FAIL internal error: {type(exc).__name__} (details suppressed)", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
