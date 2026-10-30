"""REC-056e (#135) on real Windows: idempotency keys in control.db across restarts and
processes.

Windows is where spawn, WAL and file locking differ. The ``windows`` marker only labels
these tests: they run on Linux as well, and the ci.yml ``root lock (windows-latest)`` job
selects ``tests/unit/persistence -m windows``, so they run there too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.idempotency import (
    check_concurrent_same_key_different_body,
    check_concurrent_same_key_same_body,
    check_migration_applies_once,
    check_restart_survives,
)

pytestmark = pytest.mark.windows


def test_idempotency_control_migration_applies_once_across_processes(tmp_path: Path) -> None:
    check_migration_applies_once(tmp_path)


def test_idempotency_key_survives_an_app_restart(tmp_path: Path) -> None:
    check_restart_survives(tmp_path)


def test_idempotency_concurrent_same_key_same_body_binds_once(tmp_path: Path) -> None:
    check_concurrent_same_key_same_body(tmp_path)


def test_idempotency_concurrent_same_key_different_body_binds_once(tmp_path: Path) -> None:
    check_concurrent_same_key_different_body(tmp_path)
