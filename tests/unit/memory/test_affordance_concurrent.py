"""REC-056d (#134): attempts in separate processes do not lose affordance counts.

Before, each process loaded ``affordances.json``, counted in memory and rewrote the
whole file, so the last writer won. Now each record is an SQL increment in one
``BEGIN IMMEDIATE`` transaction on the shared ``runtime.db``. The scenario lives in
``tests.support.affordance`` so the real-Windows job runs it too
(``tests/unit/persistence/test_affordance_windows.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.affordance import check_attempts_do_not_lose_increments


def test_two_attempts_do_not_lose_increments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    check_attempts_do_not_lose_increments(tmp_path, monkeypatch)
