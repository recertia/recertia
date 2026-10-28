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

Allowed addresses (REC-027). Each address must be plain ASCII; anything
else (non-UTF-8 bytes, Unicode lookalikes such as the Kelvin sign, which
lowercases to ASCII) fails before case folding. After lowercasing, an
address passes only if it is exactly one of:

* the GitHub noreply form ``<digits>+<login>@users.noreply.github.com``;
* the GitHub App bot form ``<digits>+<login>[bot]@users.noreply.github.com``
  (``[bot]`` only directly after a login that has the ``<digits>+`` prefix);
* the legacy GitHub noreply form ``<login>@users.noreply.github.com``;
* the agent address in ``_ALLOWED_EXACT``;
* GitHub's web-flow address, as committer only (merge button, squash
  merges, and "Update branch"), never as author or in a trailer.

``<login>`` follows GitHub's username rule: 1-39 characters from
``[A-Za-z0-9-]``, not starting with a hyphen. Any other local part fails,
for example ``[bot]`` without the ``<digits>+`` prefix or anywhere but
directly before the ``@``, dots, underscores, a second ``+``, or an empty
login.

Range SHAs (REC-027). ``BASE``, ``HEAD``, ``PUSH_AFTER`` and a non-zero
``PUSH_BEFORE`` must each be a 40-hex SHA, and ``BASE``, ``HEAD`` and
``PUSH_AFTER`` must resolve with ``git rev-parse --verify <sha>^{commit}``.
Anything else (a ref name, a range, an option-shaped value, a tree or blob
SHA, an unknown SHA) FAILS CLOSED (exit 1) before ``git rev-list`` runs.
The resolved commit ids, not the raw inputs, are what get checked.

Every ``<...>`` and every bare ``x@y`` token in a ``*-by`` trailer value is
checked; one non-allowlisted address fails the trailer even if another is
allowed. Each address must be a single ``local@domain`` token (no spaces,
commas, quotes, angle brackets or second ``@``). A value with unbalanced or nested
angle brackets, or with no address at all, fails closed. So does a value
that contains an at-sign lookalike anywhere, inside or outside angle
brackets: the fullwidth ``＠`` (U+FF20), the small ``﹫`` (U+FE6B), the tag
character U+E0040, or any other character whose NFKC form contains ``@``
(REC-027). Without this, ``x＠evil`` outside brackets would not be seen as
an address at all and would be skipped.

Output is redacted because CI logs are public: a failing value is reported
only as ``<sha>: <role>=non-allowlisted`` (or ``<role>=<empty>``), never the
address, its parts, or a name. Roles are ``author``, ``committer``, a known
trailer key in canonical case, or ``trailer`` for any other ``*-by`` key.
Environment inputs are echoed only if they are 40-hex SHAs or plain branch
names, otherwise as ``<invalid>``. git's own stderr is captured and
discarded, non-UTF-8 identity bytes are classified per commit, and
unexpected errors print only their type.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unicodedata

_ALLOWED_EXACT = frozenset(
    {
        "cursoragent@" + "cursor.com",
        "robs@" + "gatech.edu"
    }
)
# GitHub noreply: optional "<digits>+", then a login (GitHub's username rule:
# [A-Za-z0-9-], 1-39 chars, no leading hyphen). Matched after lowercasing.
_LOGIN = r"[a-z0-9][a-z0-9-]{0,38}"
# The App bot suffix "[bot]" is allowed only on the "<digits>+<login>" form.
_NOREPLY = re.compile(
    r"(?:[0-9]+\+" + _LOGIN + r"(?:\[bot\])?|" + _LOGIN + r")@users\.noreply\.github\.com",
    re.ASCII,
)
_WEB_FLOW_COMMITTER = "noreply@" + "github.com"
_IDENTITY_TRAILER = re.compile(r"^([A-Za-z0-9-]*-by)\s*:\s*(.*)$", re.IGNORECASE)
_ANGLE_EMAIL = re.compile(r"<([^<>]*)>")
# One local@domain token. Square brackets stay legal: GitHub App noreply
# forms look like <id>+<app>[bot]@<noreply suffix>.
_ADDRESS = re.compile(r"[^\s<>,;:\"'()@]+@[^\s<>,;:\"'()@]+")
_MALFORMED = "\x00malformed"
_KNOWN_TRAILERS = {
    key.lower(): key
    for key in (
        "Co-authored-by",
        "Signed-off-by",
        "Reviewed-by",
        "Acked-by",
        "Tested-by",
        "Reported-by",
        "Suggested-by",
        "Helped-by",
    )
}
_SHA = re.compile(r"[0-9a-f]{40}")
_BRANCH = re.compile(r"[A-Za-z0-9._/-]+")


def _allowed(email: str, kind: str = "author") -> bool:
    value = email.strip()
    # ASCII first: lowercasing could turn a lookalike into ASCII (U+212A -> "k").
    if not value or not value.isascii():
        return False
    value = value.lower()
    if not _ADDRESS.fullmatch(value):
        return False
    if value in _ALLOWED_EXACT:
        return True
    if kind == "committer" and value == _WEB_FLOW_COMMITTER:
        return True
    return _NOREPLY.fullmatch(value) is not None


_AT_LOOKALIKES = frozenset({"\uff20", "\ufe6b", "\U000e0040"})


def _has_at_lookalike(text: str) -> bool:
    """True for any non-ASCII character that is or normalizes to an at-sign."""
    return any(
        ch in _AT_LOOKALIKES or (not ch.isascii() and "@" in unicodedata.normalize("NFKC", ch)) for ch in text
    )


_ZERO_SHA = "0" * 40


def _show(rev: str, width: int = 40) -> str:
    """An env-supplied SHA for output, or ``<invalid>`` if it is not one."""
    return rev[:width] if _SHA.fullmatch(rev) else "<invalid>"


def _show_branch(name: str) -> str:
    return name if _BRANCH.fullmatch(name) else "<invalid>"


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True, stderr=subprocess.PIPE).strip()


def _resolve_commit(rev: str) -> str | None:
    """The full commit id for ``rev`` (``git rev-parse --verify``), or None."""
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    out = probe.stdout.strip()
    return out if probe.returncode == 0 and _SHA.fullmatch(out) else None


def _is_commit(rev: str) -> bool:
    return _resolve_commit(rev) is not None


class RangeError(Exception):
    """The range cannot be determined or an input is bad; the check fails closed."""


def _commit_input(name: str, value: str) -> str:
    """A range input that must be a 40-hex SHA naming a commit in this clone."""
    if not _SHA.fullmatch(value):
        raise RangeError(f"{name}={_show(value)} is not a 40-hex commit SHA; failing closed.")
    resolved = _resolve_commit(value)
    if resolved is None:
        raise RangeError(f"{name}={_show(value)} is not a commit in this clone; failing closed.")
    return resolved


def _push_revs(before: str, after: str) -> tuple[list[str], str]:
    """rev-list arguments and a label for a push; see the module docstring."""
    zero = not before or before == _ZERO_SHA
    if not zero and not _SHA.fullmatch(before):
        raise RangeError(f"PUSH_BEFORE={_show(before)} is not a 40-hex commit SHA; failing closed.")
    after = _commit_input("PUSH_AFTER", after)
    if not zero and _is_commit(before):
        before = _commit_input("PUSH_BEFORE", before)
        return [f"{before}..{after}"], f"{_show(before)}..{_show(after)}"
    default = os.environ.get("DEFAULT_BRANCH", "").strip()
    ref = os.environ.get("PUSH_REF", "").strip().removeprefix("refs/heads/")
    if not default or ref == default:
        if zero:
            raise RangeError(
                f"push creates the default branch at {_show(after, 12)}; its history is"
                " not grandfathered, so the range cannot be checked. Failing"
                " closed: a maintainer must review this push."
            )
        raise RangeError(
            f"push before={_show(before, 12)} is not in this clone (force push whose"
            " old tip could not be fetched); the pushed range cannot be"
            " computed. Failing closed instead of checking only the tip."
        )
    if not _is_commit(f"origin/{default}"):
        raise RangeError(f"origin/{_show_branch(default)} is not in this clone; failing closed.")
    try:
        base = _git("merge-base", f"origin/{default}", after)
    except subprocess.CalledProcessError as exc:
        raise RangeError(
            f"{_show(after, 12)} shares no history with origin/{_show_branch(default)}; failing closed."
        ) from exc
    if not _SHA.fullmatch(base):
        raise RangeError(f"merge-base with origin/{_show_branch(default)} is not a commit; failing closed.")
    return [f"{base}..{after}"], f"{_show(base)}..{_show(after)}"


def _range() -> tuple[str, list[str], str, bool]:
    base = os.environ.get("BASE", "").strip()
    head = os.environ.get("HEAD", "").strip()
    if base and head:
        base = _commit_input("BASE", base)
        head = _commit_input("HEAD", head)
        return head, [f"{base}..{head}"], f"{_show(base)}..{_show(head)}", False
    after = os.environ.get("PUSH_AFTER", "").strip()
    if not after:
        print(
            "Set BASE and HEAD (pull request) or PUSH_BEFORE and PUSH_AFTER (push).",
            file=sys.stderr,
        )
        return "", [], "", False
    before = os.environ.get("PUSH_BEFORE", "").strip()
    revs, label = _push_revs(before, after)
    return revs[0].rpartition("..")[2], revs, label, True


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
        errors="surrogateescape",
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
        role = _KNOWN_TRAILERS.get(match.group(1).lower(), "trailer")
        rows.extend((role, value) for value in _trailer_values(match.group(2)))
    return rows


def _trailer_values(value: str) -> list[str]:
    """Every address in one trailer value; malformed values fail closed."""
    value = value.strip()
    if not value:
        return [""]
    if _has_at_lookalike(value):
        return [_MALFORMED]
    bracketed = [inner.strip() for inner in _ANGLE_EMAIL.findall(value)]
    rest = _ANGLE_EMAIL.sub(" ", value)
    if "<" in rest or ">" in rest:
        return [_MALFORMED]
    bare = [token for token in rest.split() if "@" in token]
    return bracketed + bare or [value]


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
        bad = list(
            dict.fromkeys(
                _redacted(kind, email) for kind, email in _emails_for(sha) if not _allowed(email, kind)
            )
        )
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
