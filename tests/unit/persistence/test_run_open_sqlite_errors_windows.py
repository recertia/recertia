"""REC-096 on real Windows: a locked, malformed or read-only run database ends the run.

Windows is where SQLite file locking and read-only files differ. The ``windows`` marker
only labels these tests: they run on Linux as well, and the ci.yml
``root lock (windows-latest)`` job selects ``tests/unit/persistence -m windows``, so they
run there too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.sqlite_open_failures import SCENARIOS, check_sqlite_error_on_open_ends_the_run

pytestmark = pytest.mark.windows


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_sqlite_error_on_run_open_ends_the_run_on_windows(tmp_path: Path, scenario: str) -> None:
    check_sqlite_error_on_open_ends_the_run(tmp_path, scenario)
