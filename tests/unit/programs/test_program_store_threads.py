"""REC-112: threads that share one ``ProgramStore`` always find a program that exists.

The app holds one store per process, and the program routes are sync handlers that FastAPI
runs on a thread pool, so requests use the store's one connection from many threads at once.
The connection's statement cache hands the same prepared statement to every ``execute`` of
the same SQL text. Two threads stepping it at once reset each other's statement, so a read
could get no row for a program that exists, and the route answered 404 "program not found".
"""

from __future__ import annotations

import threading
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from contracts.program import MigrationProgram
from recertia.programs.store import ProgramStore
from tests.support.threads import run_together


def _program(title: str = "pack") -> MigrationProgram:
    return MigrationProgram(program_id="p1", tenant_id="t1", title=title)


def test_threads_sharing_one_store_always_read_a_program_that_exists(tmp_path: Path) -> None:
    store = ProgramStore(tmp_path / "programs.sqlite")
    try:
        store.put(_program())
        seen: Counter[str] = Counter()
        guard = threading.Lock()

        def note(outcome: str) -> None:
            with guard:
                seen[outcome] += 1

        def write(n: int) -> None:
            for i in range(50):
                try:
                    store.put(_program(f"w{n}-{i}"))
                    note("saved")
                except Exception as exc:  # any failure is the bug
                    note(f"put {type(exc).__name__}")

        def read() -> None:
            for _ in range(100):
                try:
                    got = store.get("p1", tenant_id="t1")
                    note("found" if got is not None else "get None")
                    listed = store.list(tenant_id="t1")
                    note("listed" if [p.program_id for p in listed] == ["p1"] else f"list {len(listed)}")
                except Exception as exc:
                    note(f"read {type(exc).__name__}")

        def list_only() -> None:
            for _ in range(200):
                try:
                    listed = store.list(tenant_id="t1")
                    note("listed" if [p.program_id for p in listed] == ["p1"] else f"list {len(listed)}")
                except Exception as exc:
                    note(f"list {type(exc).__name__}")

        writers = [lambda n=n: write(n) for n in range(2)]
        run_together(writers + [read for _ in range(4)] + [list_only for _ in range(2)])

        assert seen == {"saved": 100, "found": 400, "listed": 800}, seen
        stored = store.get("p1", tenant_id="t1")
        assert stored is not None and stored.version == 101
    finally:
        store.close()


pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tests.support.idempotency import close_app, headers_for, make_app  # noqa: E402
from tests.unit.test_migration_programs import _step  # noqa: E402


def test_concurrent_patches_and_reads_of_one_program_never_get_404(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        headers = headers_for(app)
        steps = [_step(f"s{n}", n) for n in range(4)]
        created = client.post("/v1/programs", headers=headers, json={"title": "pack", "steps": steps})
        assert created.status_code == 200, created.text
        base = f"/v1/programs/{created.json()['program']['program_id']}"
        statuses: Counter[int] = Counter()
        not_found: list[str] = []
        guard = threading.Lock()

        def note(resp: Any) -> None:
            with guard:
                statuses[resp.status_code] += 1
                if resp.status_code == 404:
                    not_found.append(f"{resp.request.method} {resp.request.url.path}: {resp.text}")

        def patch(n: int) -> None:
            for i in range(10):
                note(client.patch(f"{base}/steps/s{n}", headers=headers, json={"title": f"s{n}-{i}"}))

        def read() -> None:
            for _ in range(10):
                note(client.get(base, headers=headers))
                note(client.get("/v1/programs", headers=headers))

        run_together([lambda n=n: patch(n) for n in range(4)] + [read for _ in range(2)])

        assert not_found == []
        # A PATCH that loses the version race answers 409; every other request succeeds (no 500).
        assert set(statuses) <= {200, 409}, statuses
        assert sum(statuses.values()) == 80
    finally:
        close_app(app)
