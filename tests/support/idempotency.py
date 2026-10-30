"""REC-056e: program step-run Idempotency-Key scenarios shared by the Linux unit tests and
the ``windows``-marked module the real-Windows CI job runs.

Each ``check_*`` function is a whole test body. Imports from ``recertia`` stay inside the
functions, so a test module that imports this one still collects on a tree without the
idempotency store and fails on its assertions instead.
"""

from __future__ import annotations

import multiprocessing
import queue
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROCS = 2
# One overall deadline, under pytest-timeout's 60 s, so a hung worker is killed and
# reported here rather than orphaned when pytest-timeout fires first.
DEADLINE_S = 40.0
BARRIER_S = 30.0
TENANT = "t1"
STEP = {
    "step_id": "s1",
    "ordinal": 0,
    "title": "s1",
    "role": "custom",
    "goal": {
        "desired": [{"id": "s1-d", "kind": "file_exists", "path": "README.md", "weight": 1.0}],
        "context": "context for s1",
        "task_class": "repo-chore",
    },
    "freeze_paths": [],
    "mutate_paths": [],
}


def make_app(root: Path) -> Any:
    from recertia.api import create_app

    return create_app(root=root / "api-root", skills_root=root / "skills")


def close_app(app: Any) -> None:
    """Release what a process would release on exit, so the next app reopens the files."""

    state = app.state.recertia
    close = getattr(state, "close", None)  # AppState.close arrived with REC-056e
    if close is not None:
        close()
    state.queue.close()
    state.programs.close()


def headers_for(app: Any, tenant_id: str = TENANT) -> dict[str, str]:
    issued = app.state.api_keys.issue(
        tenant_id=tenant_id, scopes={"runs", "metrics", "exec", "admin"}, actor="test"
    )
    return {"X-API-Key": issued.secret}


def solved_run(run_id: str, criteria_hash: str, *, tenant_id: str = TENANT, status: str = "solved") -> Any:
    from recertia.api.schemas import RunRecord

    return RunRecord(
        run_id=run_id,
        request="x",
        task_class="repo-chore",
        status=status,
        created_at=datetime.now(timezone.utc),
        tenant_id=tenant_id,
        terminal=status,
        has_goal=True,
        criteria_hash=criteria_hash,
    )


def previewed_program(
    client: Any, headers: dict[str, str], steps: list[dict[str, Any]] | None = None
) -> tuple[str, str]:
    """An accepted program whose first step is previewed: ``(program_id, preview hash)``."""

    created = client.post("/v1/programs", headers=headers, json={"title": "pack", "steps": steps or [STEP]})
    assert created.status_code == 200, created.text
    pid = str(created.json()["program"]["program_id"])
    accepted = client.post(f"/v1/programs/{pid}/accept", headers=headers, json={"ack_disclaimer": True})
    assert accepted.status_code == 200, accepted.text
    preview = client.post(f"/v1/programs/{pid}/steps/s1/preview", headers=headers)
    assert preview.status_code == 200, preview.text
    return pid, str(preview.json()["criteria_preview_hash"])


def key_rows(root: Path) -> list[tuple[Any, ...]]:
    """Every ``idempotency_keys`` row of ``root``'s control.db, read on a fresh connection."""

    conn = sqlite3.connect(root / "api-root" / "control.db")
    try:
        return list(
            conn.execute(
                "SELECT tenant_id, program_id, step_id, idempotency_key, state, run_id"
                " FROM idempotency_keys ORDER BY tenant_id, program_id, step_id, idempotency_key"
            )
        )
    finally:
        conn.close()


def check_restart_survives(tmp_path: Path) -> None:
    """A keyed bind replays its run_id from a new app built on the same root."""

    from fastapi.testclient import TestClient

    app = make_app(tmp_path)
    try:
        client = TestClient(app)
        headers = headers_for(app)
        pid, ph = previewed_program(client, headers)
        app.state.runs[(TENANT, "run-1")] = solved_run("run-1", ph)
        body = {"bind_run_id": "run-1", "workdir": "ws", "idempotency_key": "k-restart"}
        first = client.post(f"/v1/programs/{pid}/steps/s1/run", headers=headers, json=body)
        assert first.status_code == 200, first.text
        assert (first.json()["run_id"], first.json()["idempotent"]) == ("run-1", False)
        # Control: the same process still answers the replay.
        again = client.post(f"/v1/programs/{pid}/steps/s1/run", headers=headers, json=body)
        assert again.status_code == 200, again.text
        assert (again.json()["run_id"], again.json()["idempotent"]) == ("run-1", True)
    finally:
        close_app(app)

    restarted = make_app(tmp_path)
    try:
        assert restarted is not app and restarted.state.recertia is not app.state.recertia
        replay = TestClient(restarted).post(
            f"/v1/programs/{pid}/steps/s1/run", headers=headers_for(restarted), json=body
        )
        outcome = (replay.status_code, replay.json().get("run_id"), replay.json().get("idempotent"))
        assert outcome == (200, "run-1", True), replay.text
    finally:
        close_app(restarted)


# --- cross-process binds through ProgramService --------------------------------------


def _bind_worker(
    root: str, pid: str, body: dict[str, Any], runs: dict[str, str], worker: int, barrier: Any, out: Any
) -> None:
    """One process: its own stores on the shared files, then the service's keyed bind."""

    try:
        from types import SimpleNamespace

        from recertia.api.schemas import StepRunBody
        from recertia.app.programs import ProgramService
        from recertia.errors import RecertiaError
        from recertia.persistence.idempotency import IdempotencyStore
        from recertia.programs.store import ProgramStore

        base = Path(root) / "api-root"
        programs = ProgramStore(base / "programs.sqlite")
        store = IdempotencyStore(base / "control.db")
        try:
            runtime = SimpleNamespace(
                root=base,
                programs=programs,
                idempotency=store,
                runs={(TENANT, rid): solved_run(rid, ph) for rid, ph in runs.items()},
                load_from_checkpoints=lambda *_args: None,
            )
            service = ProgramService()
            deadline = time.monotonic() + DEADLINE_S / 2
            barrier.wait()
            while True:
                try:
                    result = service.run_step(
                        None,  # type: ignore[arg-type]
                        tenant=TENANT,
                        runtime=runtime,
                        program_id=pid,
                        step_id="s1",
                        body=StepRunBody(**body),
                    )
                except RecertiaError as exc:
                    if exc.code == "idempotency_in_progress" and time.monotonic() < deadline:
                        time.sleep(0.05)
                        continue
                    out.put((worker, "error", exc.code))
                    return
                out.put((worker, "ok", (result["run_id"], result["idempotent"])))
                return
        finally:
            store.close()
            programs.close()
    except BaseException as exc:  # pragma: no cover - reported to the parent
        out.put((worker, "crash", f"{type(exc).__name__}: {exc}"))


def _race(
    tmp_path: Path, bodies: list[dict[str, Any]], runs: dict[str, str], pid: str
) -> list[tuple[Any, ...]]:
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(len(bodies), timeout=BARRIER_S)
    out = ctx.Queue()
    procs = [
        ctx.Process(target=_bind_worker, args=(str(tmp_path), pid, body, runs, n, barrier, out))
        for n, body in enumerate(bodies)
    ]
    deadline = time.monotonic() + DEADLINE_S
    for proc in procs:
        proc.start()
    results: list[tuple[Any, ...]] = []
    while len(results) < len(procs) and time.monotonic() < deadline:
        try:
            results.append(out.get(timeout=max(0.1, min(1.0, deadline - time.monotonic()))))
        except queue.Empty:
            continue
    for proc in procs:
        proc.join(max(0.0, deadline - time.monotonic()))
    stragglers = [proc for proc in procs if proc.is_alive()]
    for proc in stragglers:
        proc.kill()
        proc.join(timeout=5)
    assert not stragglers, "a worker hung"
    assert [proc.exitcode for proc in procs] == [0] * len(procs)
    assert len(results) == len(procs), results
    return sorted(results)


def _setup_race(tmp_path: Path) -> tuple[str, str]:
    from fastapi.testclient import TestClient

    app = make_app(tmp_path)
    try:
        pid, ph = previewed_program(TestClient(app), headers_for(app))
    finally:
        close_app(app)
    return pid, ph


def _stored_step(tmp_path: Path, pid: str) -> Any:
    from recertia.programs.store import ProgramStore

    programs = ProgramStore(tmp_path / "api-root" / "programs.sqlite")
    try:
        prog = programs.get(pid, tenant_id=TENANT)
        assert prog is not None
        return prog, next(s for s in prog.steps if s.step_id == "s1")
    finally:
        programs.close()


def check_concurrent_same_key_same_body(tmp_path: Path) -> None:
    """2 processes, one key, one body: one complete row, and both end with the same run_id."""

    pid, ph = _setup_race(tmp_path)
    body = {"bind_run_id": "run-1", "workdir": "ws", "idempotency_key": "k-race"}
    results = _race(tmp_path, [body] * PROCS, {"run-1": ph}, pid)
    assert [r[1] for r in results] == ["ok"] * PROCS, results
    assert {r[2][0] for r in results} == {"run-1"}
    assert sorted(r[2][1] for r in results) == [False, True]
    assert key_rows(tmp_path) == [(TENANT, pid, "s1", "k-race", "complete", "run-1")]
    prog, step = _stored_step(tmp_path, pid)
    assert (step.current_run_id, step.run_ids) == ("run-1", ["run-1"])


def check_concurrent_same_key_different_body(tmp_path: Path) -> None:
    """2 processes, one key, two bodies: exactly one binds, the other gets idempotency_mismatch."""

    pid, ph = _setup_race(tmp_path)
    bodies = [{"bind_run_id": f"run-{n}", "workdir": "ws", "idempotency_key": "k-race"} for n in range(PROCS)]
    results = _race(tmp_path, bodies, {f"run-{n}": ph for n in range(PROCS)}, pid)
    oks = [r for r in results if r[1] == "ok"]
    errors = [r for r in results if r[1] == "error"]
    assert len(oks) == 1 and [e[2] for e in errors] == ["idempotency_mismatch"], results
    winner = oks[0][2][0]
    assert oks[0][2][1] is False
    assert key_rows(tmp_path) == [(TENANT, pid, "s1", "k-race", "complete", winner)]
    prog, step = _stored_step(tmp_path, pid)
    assert (step.current_run_id, step.run_ids) == (winner, [winner])


# --- migration 003 applied by concurrent openers --------------------------------------


def _migrate_worker(path: str, worker: int, barrier: Any, out: Any) -> None:
    try:
        from recertia.persistence import idempotency

        # Migrate the way the server does: IdempotencyStore's first write opens control.db
        # through _writer(). Record what apply_migrations reports so the parent can check
        # that each control migration is applied exactly once across processes.
        real_apply = idempotency.apply_migrations
        applied: list[str] = []

        def recording_apply(conn: Any, directory: Any) -> list[str]:
            result = real_apply(conn, directory)
            applied.extend(result)
            return result

        idempotency.apply_migrations = recording_apply  # type: ignore[assignment]
        store = idempotency.IdempotencyStore(path)
        barrier.wait()
        try:
            store._writer()
            out.put((worker, "ok", applied))
        finally:
            store.close()
    except BaseException as exc:  # pragma: no cover - reported to the parent
        out.put((worker, "crash", f"{type(exc).__name__}: {exc}"))


def check_migration_applies_once(tmp_path: Path, procs: int = 3) -> None:
    """``procs`` spawned processes open one fresh control.db at once: 003 is applied once."""

    path = tmp_path / "control.db"
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(procs, timeout=BARRIER_S)
    out = ctx.Queue()
    workers = [ctx.Process(target=_migrate_worker, args=(str(path), n, barrier, out)) for n in range(procs)]
    deadline = time.monotonic() + DEADLINE_S
    for proc in workers:
        proc.start()
    for proc in workers:
        proc.join(max(0.0, deadline - time.monotonic()))
    stragglers = [proc for proc in workers if proc.is_alive()]
    for proc in stragglers:
        proc.kill()
        proc.join(timeout=5)
    assert not stragglers, "a worker hung"
    results = sorted(out.get(timeout=5) for _ in workers)
    assert [r[1] for r in results] == ["ok"] * procs, results
    applied = [version for r in results for version in r[2]]
    assert sorted(applied) == ["001_baseline", "002_queue", "003_idempotency"], results
    conn = sqlite3.connect(path)
    try:
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        assert versions == ["001_baseline", "002_queue", "003_idempotency"]
        columns = [row[1] for row in conn.execute("PRAGMA table_info(idempotency_keys)")]
        assert "expires_at" in columns
    finally:
        conn.close()
