"""REC-056d-pre2 on real Windows: store migrations (``recertia.store``) apply once when
processes open the same new store db at once, and a db from the earlier store runner
still opens.

Windows is where spawn, WAL and file locking differ. The ``windows`` marker only labels
these tests: they run on Linux as well, and the ci.yml ``root lock (windows-latest)``
job selects ``tests/unit/persistence -m windows``, so they run there too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.store_migrations import (
    check_apply_closes_its_connection,
    check_concurrent_new_store_dbs_apply_each_migration_once,
    check_legacy_store_db_is_backfilled,
)

pytestmark = pytest.mark.windows


def test_store_migrations_concurrent_new_dbs_apply_each_migration_once(tmp_path: Path) -> None:
    check_concurrent_new_store_dbs_apply_each_migration_once(tmp_path)


def test_store_migrations_legacy_db_is_backfilled_and_nothing_is_created(tmp_path: Path) -> None:
    check_legacy_store_db_is_backfilled(tmp_path)


def test_store_migrations_apply_closes_its_connection_on_success_and_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    check_apply_closes_its_connection(tmp_path, monkeypatch)
