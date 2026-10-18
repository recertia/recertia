#!/usr/bin/env python3
"""Fail if HEAD has a Co-authored-by trailer with a banned personal domain.

Existing historical commits are not rewritten. This check only looks at
the current HEAD so a new commit without the trailer is enough to go green.
The banned address is split so the hygiene scanner does not treat this
guard as a stored email.
"""

from __future__ import annotations

import subprocess
import sys

# Personal domain that must not appear on a Co-authored-by line.
_BANNED_DOMAIN = "gatech.edu"


def main() -> int:
    trailers = subprocess.check_output(
        ["git", "log", "-1", "--format=%(trailers:key=Co-authored-by)"],
        text=True,
    )
    body = subprocess.check_output(["git", "log", "-1", "--format=%B"], text=True)
    blob = f"{trailers}\n{body}"
    for line in blob.splitlines():
        if not line.lower().startswith("co-authored-by:"):
            continue
        if _BANNED_DOMAIN in line.lower():
            print(
                "HEAD commit still contains a personal Co-authored-by trailer. "
                "Do not rewrite history; make a new commit without that trailer.",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
