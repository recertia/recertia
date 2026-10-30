"""REC-056d (#134) on real Windows: the affordance store's cross-process increments
and its never-overwrite renames.

Windows is where rename and lock semantics differ (``os.replace`` onto a reserved
placeholder, sharing violations, spawn with WAL). The ``windows`` marker only labels
these tests: they run on Linux as well, and the ci.yml ``root lock (windows-latest)``
job selects ``tests/unit/persistence -m windows``, so they run there too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.affordance import (
    check_attempts_do_not_lose_increments,
    check_corrupt_quarantine_never_overwrites,
    check_imported_rename_never_overwrites,
    check_reader_marker_error_raises,
)

pytestmark = pytest.mark.windows


def test_affordance_attempts_do_not_lose_increments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    check_attempts_do_not_lose_increments(tmp_path, monkeypatch)


def test_affordance_corrupt_quarantine_never_overwrites_an_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    check_corrupt_quarantine_never_overwrites(tmp_path, monkeypatch)


def test_affordance_imported_rename_never_overwrites_an_existing_file(tmp_path: Path) -> None:
    check_imported_rename_never_overwrites(tmp_path)


def test_affordance_reader_marker_error_raises_instead_of_reading_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    check_reader_marker_error_raises(tmp_path, monkeypatch)
