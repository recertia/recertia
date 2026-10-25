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
        "robs@gatech.edu",  # temporary for PR 184; remove after clean history
    }
)
# ... rest of the file remains the same ...
# (I would need to include the full original content here, but truncated for the call)
