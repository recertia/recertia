"""REC-056d-pre2: store migration scenarios shared by the runtime tests and the
``windows``-marked module the real-Windows CI job runs.

Each ``check_*`` function is a whole test body.
"""

from __future__ import annotations

import hashlib
import multiprocessing
import queue
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

PROCS = 4
ROUNDS = 64
BARRIER_S = 30.0
# One overall deadline, under pytest-timeout's 60 s, so a hung worker is killed and
# reported here rather than orphaned when pytest-timeout fires first.
DEADLINE_S = 40.0


def _store_db(root: Path, index: int) -> Path:
    return root / f"round-{index}" / "store.sqlite"


def _apply_in_rounds(root: str, rounds: int, barrier: Any, out: Any, migrations_dir: str | None) -> None:
    """Child: for each round, wait for every process, then migrate that round's db."""

    import recertia.store as store

    if migrations_dir is not None:
        store.MIGRATIONS_DIR = Path(migrations_dir)
    for index in range(rounds):
        barrier.wait(BARRIER_S)
        try:
            out.put((index, "ok", store.apply_sqlite_migrations(_store_db(Path(root), index))))
        except Exception as exc:  # reported to the parent, which fails the test on it
            out.put((index, "error", f"{type(exc).__name__}: {exc}"))


def _versions(db: Path) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return [str(row[0]) for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    finally:
        conn.close()


def _race(root: Path, migrations_dir: Path | None) -> list[tuple[int, str, Any]]:
    """``PROCS`` spawned processes open each of ``ROUNDS`` dbs under ``root`` at the same moment.

    Fails the test on any child error, missing report or non-zero exit code.
    """

    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(PROCS)
    out = ctx.Queue()
    child_dir = None if migrations_dir is None else str(migrations_dir)
    workers = [
        ctx.Process(
            target=_apply_in_rounds, args=(str(root), ROUNDS, barrier, out, child_dir), name=f"store-{i}"
        )
        for i in range(PROCS)
    ]
    for worker in workers:
        worker.start()
    results: list[tuple[int, str, Any]] = []
    deadline = time.monotonic() + DEADLINE_S
    try:
        while len(results) < PROCS * ROUNDS and time.monotonic() < deadline:
            try:
                results.append(out.get(timeout=max(0.1, deadline - time.monotonic())))
            except queue.Empty:
                break
        for worker in workers:
            worker.join(max(0.0, deadline - time.monotonic()))
    finally:
        for worker in workers:
            if worker.is_alive():  # never leave a stuck child to hang interpreter exit
                worker.kill()
                worker.join(5)

    errors = sorted(r for r in results if r[1] != "ok")
    assert errors == [], f"concurrent apply_sqlite_migrations failed: {errors}"
    assert len(results) == PROCS * ROUNDS, f"{len(results)} of {PROCS * ROUNDS} calls reported"
    assert [w.exitcode for w in workers] == [0] * PROCS
    return results


def _assert_each_applied_once(
    root: Path, results: list[tuple[int, str, Any]], new: list[str], on_disk: list[str]
) -> None:
    for index in range(ROUNDS):
        applied = sorted(v for r in results if r[0] == index for v in r[2])
        assert applied == new, f"round {index}: applied {applied}, expected each of {new} once"
        assert _versions(_store_db(root, index)) == on_disk


def check_concurrent_new_store_dbs_apply_each_migration_once(tmp_path: Path) -> None:
    """``PROCS`` spawned processes open each of ``ROUNDS`` new store dbs at the same moment."""

    from recertia.store import list_migrations

    expected = sorted(path.name.split(".")[0] for path in list_migrations(dialect="sqlite"))
    assert expected, "no store migrations found"
    _assert_each_applied_once(tmp_path, _race(tmp_path, None), expected, expected)


def check_concurrent_existing_store_dbs_apply_an_added_migration_once(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Existing store dbs, then a new migration file: ``PROCS`` processes open each db at once.

    Here ``schema_migrations`` already exists, so the first statement of the locked pass
    is a read; only a write lock taken at ``BEGIN`` keeps two processes from both
    deciding the new file is pending.
    """

    import recertia.store as store

    migrations = tmp_path / "migrations"
    shutil.copytree(store.MIGRATIONS_DIR, migrations)
    monkeypatch.setattr(store, "MIGRATIONS_DIR", migrations)
    first = sorted(path.name.split(".")[0] for path in migrations.glob("*.sqlite.sql"))
    dbs = tmp_path / "dbs"
    for index in range(ROUNDS):
        assert store.apply_sqlite_migrations(_store_db(dbs, index)) == first
    (migrations / "999_added.sqlite.sql").write_text(
        "CREATE TABLE rec056d_added (id INTEGER PRIMARY KEY);\n", encoding="utf-8"
    )
    results = _race(dbs, migrations)
    _assert_each_applied_once(dbs, results, ["999_added"], [*first, "999_added"])


def _objects(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return [
        (str(r[0]), str(r[1]), str(r[2]))
        for r in conn.execute(
            "SELECT type, name, COALESCE(sql, '') FROM sqlite_master"
            " WHERE name != 'schema_migrations' ORDER BY type, name"
        )
    ]


def check_legacy_store_db_is_backfilled(tmp_path: Path) -> None:
    """A db the earlier store runner wrote (``version``, ``applied_at`` only) opens unchanged."""

    from recertia.store import apply_sqlite_migrations, list_migrations

    db = tmp_path / "legacy" / "store.sqlite"
    db.parent.mkdir(parents=True)
    files = list_migrations(dialect="sqlite")
    assert files, "no store migrations found"
    conn = sqlite3.connect(db)
    try:
        # The earlier runner's table and rows, written the way it wrote them.
        conn.execute(
            "CREATE TABLE schema_migrations ("
            "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        for path in files:
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (path.name.split(".")[0],))
        conn.commit()
        before = _objects(conn)
        applied_at = dict(conn.execute("SELECT version, applied_at FROM schema_migrations").fetchall())
    finally:
        conn.close()

    assert apply_sqlite_migrations(db) == []

    conn = sqlite3.connect(db)
    try:
        assert _objects(conn) == before, "the call created or changed a table"
        rows = {
            r[0]: (r[1], r[2])
            for r in conn.execute("SELECT version, applied_at, checksum FROM schema_migrations")
        }
    finally:
        conn.close()
    assert rows == {
        path.name.split(".")[0]: (
            applied_at[path.name.split(".")[0]],
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in files
    }
