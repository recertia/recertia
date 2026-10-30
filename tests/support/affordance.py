"""REC-056d: affordance-store scenarios shared by the Linux unit tests and the
``windows``-marked module the real-Windows CI job runs.

Each ``check_*`` function is a whole test body; the test modules call them.
"""

from __future__ import annotations

import json
import multiprocessing
import sqlite3
import time
from pathlib import Path

import pytest

PROCS = 3
CALLS = 50
CAP = 8
SEED = 5
# One overall deadline, under pytest-timeout's 60 s, so a hung worker is killed and
# reported here rather than orphaned when pytest-timeout fires first.
DEADLINE_S = 40.0
BARRIER_S = 30.0


def _attempt(path: str, worker: int, barrier: object, errors: object) -> None:
    try:
        from recertia.memory.affordance import AffordanceStore
        from recertia.solver.tools import ToolResult

        store = AffordanceStore(Path(path))
        try:
            barrier.wait()  # type: ignore[attr-defined]
            for i in range(CALLS):
                store.record_tool(
                    ToolResult(tool="t", ok=i % 2 == 0, duration_s=float(i), error_signature=f"w{worker}-{i}")
                )
                store.save()
        finally:
            store.close()
    except BaseException as exc:  # pragma: no cover - reported to the parent
        errors.put(f"{worker}: {type(exc).__name__}: {exc}")  # type: ignore[attr-defined]


def check_attempts_do_not_lose_increments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """3 spawned processes record 50 times each on one path after a barrier."""

    monkeypatch.setenv("RECERTIA_AFFORDANCE_SAMPLE_CAP", str(CAP))
    path = tmp_path / "affordances.json"
    seed = {
        "tools": {
            "t": {
                "tool": "t",
                "invocations": SEED,
                "failures": 0,
                "durations_s": [],
                "costs_usd": [],
                "error_signatures": {},
            }
        },
        "resources": {},
    }
    path.write_text(json.dumps(seed), encoding="utf-8")

    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(PROCS, timeout=BARRIER_S)
    errors = ctx.Queue()
    procs = [ctx.Process(target=_attempt, args=(str(path), w, barrier, errors)) for w in range(PROCS)]
    deadline = time.monotonic() + DEADLINE_S
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(max(0.0, deadline - time.monotonic()))
    stragglers = [proc for proc in procs if proc.is_alive()]
    for proc in stragglers:
        proc.kill()
        proc.join(timeout=5)
    found = []
    while not errors.empty():
        found.append(errors.get())
    assert not stragglers, "a worker hung"
    assert found == []
    assert [proc.exitcode for proc in procs] == [0] * PROCS

    from recertia.memory.affordance import AffordanceStore

    reader = AffordanceStore(path)
    agg = reader.tool("t")
    assert agg is not None
    assert (agg.invocations, agg.failures) == (SEED + PROCS * CALLS, PROCS * (CALLS // 2))
    assert len(agg.error_signatures) <= CAP
    assert len(agg.durations_s) <= CAP

    conn = sqlite3.connect(tmp_path / "runtime.db")
    try:
        (sigs,) = conn.execute("SELECT COUNT(*) FROM affordance_error_signatures").fetchone()
        assert sigs <= CAP
        (samples,) = conn.execute("SELECT COUNT(*) FROM affordance_tool_samples").fetchone()
        assert samples <= CAP
        (markers,) = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version='runtime.import_affordances_json'"
        ).fetchone()
        assert markers == 1
    finally:
        conn.close()
    assert not path.exists()
    assert (tmp_path / "affordances.json.imported").is_file()


def _legacy(path: Path, invocations: int) -> None:
    body = {
        "tools": {
            "t": {
                "tool": "t",
                "invocations": invocations,
                "failures": 0,
                "durations_s": [],
                "costs_usd": [],
                "error_signatures": {},
            }
        },
        "resources": {},
    }
    path.write_text(json.dumps(body), encoding="utf-8")


def check_corrupt_quarantine_never_overwrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A torn file goes to a fresh ``.corrupt-<ns>-<n>`` name when ``.corrupt-<ns>`` is taken."""

    from recertia.memory import affordance as affordance_mod
    from recertia.solver.tools import ToolResult

    monkeypatch.setattr(affordance_mod.time, "time_ns", lambda: 123)
    path = tmp_path / "affordances.json"
    path.write_text("{torn", encoding="utf-8")
    taken = tmp_path / "affordances.json.corrupt-123"
    taken.write_text("earlier", encoding="utf-8")
    store = affordance_mod.AffordanceStore(path)
    try:
        store.record_tool(ToolResult(tool="t", ok=True))
    finally:
        store.close()
    assert taken.read_text(encoding="utf-8") == "earlier"
    assert (tmp_path / "affordances.json.corrupt-123-1").read_text(encoding="utf-8") == "{torn"
    assert not path.exists()
    conn = sqlite3.connect(tmp_path / "runtime.db")
    try:
        rows = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version=?", (affordance_mod.IMPORT_MARKER,)
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("corrupt",)]


def check_imported_rename_never_overwrites(tmp_path: Path) -> None:
    """An imported file goes to a fresh ``.imported-<ns>`` name when ``.imported`` is taken."""

    from recertia.memory.affordance import AffordanceStore

    path = tmp_path / "affordances.json"
    _legacy(path, invocations=1)
    taken = tmp_path / "affordances.json.imported"
    taken.write_text("earlier", encoding="utf-8")
    store = AffordanceStore(path)
    try:
        store.save()
    finally:
        store.close()
    assert taken.read_text(encoding="utf-8") == "earlier"
    assert len(list(tmp_path.glob("affordances.json.imported-*"))) == 1
    assert not path.exists()
