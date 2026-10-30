"""REC-056d (#134): attempts in separate processes do not lose affordance counts.

Before, each process loaded ``affordances.json``, counted in memory and rewrote the
whole file, so the last writer won. Now each record is an SQL increment in one
``BEGIN IMMEDIATE`` transaction on the shared ``runtime.db``.
"""

from __future__ import annotations

import json
import multiprocessing
import sqlite3
from pathlib import Path

_PROCS = 3
_CALLS = 50
_CAP = 8
_SEED = 5


def _attempt(path: str, worker: int, barrier: object, errors: object) -> None:
    try:
        from recertia.memory.affordance import AffordanceStore
        from recertia.solver.tools import ToolResult

        store = AffordanceStore(Path(path))
        barrier.wait()  # type: ignore[attr-defined]
        for i in range(_CALLS):
            store.record_tool(
                ToolResult(tool="t", ok=i % 2 == 0, duration_s=float(i), error_signature=f"w{worker}-{i}")
            )
            store.save()
    except BaseException as exc:  # pragma: no cover - reported to the parent
        errors.put(f"{worker}: {type(exc).__name__}: {exc}")  # type: ignore[attr-defined]


def test_two_attempts_do_not_lose_increments(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RECERTIA_AFFORDANCE_SAMPLE_CAP", str(_CAP))
    path = tmp_path / "affordances.json"
    seed = {
        "tools": {
            "t": {
                "tool": "t",
                "invocations": _SEED,
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
    barrier = ctx.Barrier(_PROCS, timeout=60)
    errors = ctx.Queue()
    procs = [ctx.Process(target=_attempt, args=(str(path), w, barrier, errors)) for w in range(_PROCS)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=120)
    stragglers = [proc for proc in procs if proc.is_alive()]
    for proc in stragglers:
        proc.kill()
        proc.join(timeout=10)
    found = []
    while not errors.empty():
        found.append(errors.get())
    assert not stragglers, "a worker hung"
    assert found == []
    assert [proc.exitcode for proc in procs] == [0] * _PROCS

    from recertia.memory.affordance import AffordanceStore

    reader = AffordanceStore(path)
    agg = reader.tool("t")
    assert agg is not None
    assert (agg.invocations, agg.failures) == (_SEED + _PROCS * _CALLS, _PROCS * (_CALLS // 2))
    assert len(agg.error_signatures) <= _CAP
    assert len(agg.durations_s) <= _CAP

    conn = sqlite3.connect(tmp_path / "runtime.db")
    try:
        (sigs,) = conn.execute("SELECT COUNT(*) FROM affordance_error_signatures").fetchone()
        assert sigs <= _CAP
        (samples,) = conn.execute("SELECT COUNT(*) FROM affordance_tool_samples").fetchone()
        assert samples <= _CAP
        (markers,) = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version='runtime.import_affordances_json'"
        ).fetchone()
        assert markers == 1
    finally:
        conn.close()
    assert not path.exists()
    assert (tmp_path / "affordances.json.imported").is_file()
