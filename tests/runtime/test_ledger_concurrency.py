"""Concurrent ledger appends keep one contiguous chain."""

from __future__ import annotations

import multiprocessing
import threading
import time
from pathlib import Path
from typing import Any, Final

from recertia.ledger import HashChainLedger

# REC-017: the appenders are started with ``spawn``, not Linux's default ``fork``.
# The pytest process is multi-threaded while a test runs (at least
# faulthandler_timeout's watchdog; on a cold .grimp_cache also grimp's rayon
# pool), and a forked child inherits every lock in the state some other thread
# holds it at that instant. A spawned child starts from a fresh interpreter.
_APPENDER_START_METHOD: Final = "spawn"
_START_TIMEOUT_S: Final = 20.0  # a spawned child imports recertia first; this bounds the wait for the others


def _append_many(path: str, count: int, actor: str, start: Any = None) -> None:
    if start is not None:
        start.wait(_START_TIMEOUT_S)  # every appender starts appending together
    ledger = HashChainLedger(path)
    for index in range(count):
        ledger.append(actor=actor, action="write", target=f"{actor}-{index}")


def _run_appenders(path: Path, *, procs: int, count: int, deadline_s: float = 40.0) -> list[int | None]:
    """Run ``procs`` spawned appender processes at once; exit codes (None = killed at the deadline).

    A barrier releases all appenders together, so their appends overlap even
    though spawned children start one by one. One deadline for all joins, under
    the suite's 60s pytest-timeout; a child still alive at it is killed so it
    cannot hang interpreter exit.
    """

    ctx = multiprocessing.get_context(_APPENDER_START_METHOD)
    start = ctx.Barrier(procs)
    workers = [
        ctx.Process(target=_append_many, args=(str(path), count, f"p{i}", start)) for i in range(procs)
    ]
    for worker in workers:
        worker.start()
    deadline = time.monotonic() + deadline_s
    for worker in workers:
        worker.join(max(0.0, deadline - time.monotonic()))
    codes = [worker.exitcode for worker in workers]
    for worker in workers:
        if worker.is_alive():
            worker.kill()
            worker.join(5)
    return codes


def test_parallel_appends_two_instances_keep_chain_valid(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    errors: list[BaseException] = []

    def worker(actor: str) -> None:
        try:
            _append_many(str(path), 200, actor)
        except BaseException as exc:  # noqa: BLE001 — surface worker failures
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert errors == []
    ledger = HashChainLedger(path)
    report = ledger.verify(full=True)
    assert report.ok
    assert report.entries == 400
    assert [entry.seq for entry in ledger.entries()] == list(range(400))


def test_parallel_appends_four_processes_keep_chain_valid(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    codes = _run_appenders(path, procs=4, count=40)
    assert codes == [0, 0, 0, 0], f"appender exit codes {codes} (None: stuck, killed at deadline)"
    text = path.read_text(encoding="utf-8")
    assert "entry at position" not in text
    ledger = HashChainLedger(path)
    report = ledger.verify(full=True)
    assert report.entries == 160
    entries = ledger.entries()
    assert [entry.seq for entry in entries] == list(range(160))
    # Real concurrency: the processes' appends interleave. Run one after another
    # they would give 3 actor switches; together they give dozens.
    actors = [entry.actor for entry in entries]
    switches = sum(1 for before, after in zip(actors, actors[1:]) if before != after)
    assert switches > 3, f"appenders did not overlap ({switches} actor switches)"


def test_parallel_appenders_ignore_locks_held_by_parent_threads(tmp_path: Path) -> None:
    """A lock another thread of this process holds while the appenders start must not wedge them.

    Deterministic stand-in for the fork hazard: a thread holds the file-lock
    registry guard (taken by every ``HashChainLedger.append``) while the
    appenders start. A forked child would inherit it locked and block until the
    deadline; a spawned child has its own.
    """

    from recertia.fsutil import locks

    path = tmp_path / "ledger.jsonl"
    held = threading.Event()
    release = threading.Event()

    def _hold_guard() -> None:
        with locks._GUARD:
            held.set()
            release.wait(30)

    holder = threading.Thread(target=_hold_guard, name="rec017-guard-holder", daemon=True)
    holder.start()
    assert held.wait(5)
    try:
        codes = _run_appenders(path, procs=2, count=3, deadline_s=15)
    finally:
        release.set()
        holder.join(5)
    assert codes == [0, 0], f"appender exit codes {codes} (None: stuck on an inherited lock)"
    assert [entry.seq for entry in HashChainLedger(path).entries()] == list(range(6))
