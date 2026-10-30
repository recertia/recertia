"""Two processes that open the same fresh runtime.db apply each migration once."""

from __future__ import annotations

import multiprocessing as mp
import sqlite3
import time
from pathlib import Path
from typing import Any, Final

from recertia.persistence.db import apply_migrations, open_db

_RUNTIME_MIGRATIONS: Final = (
    Path(__file__).resolve().parents[3] / "src" / "recertia" / "persistence" / "migrations" / "runtime"
)
_PROCS: Final = 4
_ROUNDS: Final = 8
_BARRIER_TIMEOUT_S: Final = 30.0
_DEADLINE_S: Final = 40.0  # under pytest-timeout's 60 s, so the kill/join cleanup runs


def _open_and_migrate(root: str, migrations: str, rounds: int, barrier: object, out: object) -> None:
    """Child: for each round, wait for every process, then migrate the round's fresh db."""

    for index in range(rounds):
        barrier.wait(_BARRIER_TIMEOUT_S)  # type: ignore[attr-defined]
        try:
            conn = open_db(Path(root) / f"round-{index}" / "runtime.db")
            try:
                applied = apply_migrations(conn, Path(migrations))
            finally:
                conn.close()
            out.put((index, "ok", applied))  # type: ignore[attr-defined]
        except Exception as exc:  # report every failure kind to the parent
            out.put((index, "error", f"{type(exc).__name__}: {exc}"))  # type: ignore[attr-defined]


def _versions(db: Path) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return [str(row[0]) for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    finally:
        conn.close()


def test_concurrent_fresh_opens_apply_each_migration_once(tmp_path: Path) -> None:
    expected = sorted(
        line.split(":", 1)[1].strip()
        for path in _RUNTIME_MIGRATIONS.glob("*.sqlite.sql")
        for line in path.read_text(encoding="utf-8").splitlines()[:1]
        if line.startswith("-- id:")
    )
    assert len(expected) >= 3

    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(_PROCS)
    out = ctx.Queue()
    workers = [
        ctx.Process(
            target=_open_and_migrate,
            args=(str(tmp_path), str(_RUNTIME_MIGRATIONS), _ROUNDS, barrier, out),
            name=f"migrate-{i}",
        )
        for i in range(_PROCS)
    ]
    for worker in workers:
        worker.start()
    results: list[tuple[int, str, Any]] = []
    deadline = time.monotonic() + _DEADLINE_S
    try:
        while len(results) < _PROCS * _ROUNDS and time.monotonic() < deadline:
            try:
                results.append(out.get(timeout=max(0.1, deadline - time.monotonic())))
            except Exception:
                break
        for worker in workers:
            worker.join(max(0.0, deadline - time.monotonic()))
    finally:
        for worker in workers:
            if worker.is_alive():  # never leave a stuck child to hang interpreter exit
                worker.kill()
                worker.join(5)

    errors = [r for r in results if r[1] != "ok"]
    assert errors == [], f"concurrent apply_migrations failed: {errors}"
    assert [w.exitcode for w in workers] == [0] * _PROCS
    assert len(results) == _PROCS * _ROUNDS
    for index in range(_ROUNDS):
        applied = sorted(v for r in results if r[0] == index for v in r[2])
        assert applied == expected, f"round {index}: applied {applied}, expected each of {expected} once"
        assert _versions(tmp_path / f"round-{index}" / "runtime.db") == expected
