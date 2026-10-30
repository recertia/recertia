"""REC-096: a run whose own database cannot be opened, shared by the worker tests and
the ``windows``-marked module the real-Windows CI job runs.

``AsyncRunWorker._run`` opens the run's persistence (``runs/<tenant>/runtime.db``)
through ``build_default_orchestrator``. Each scenario below makes that open raise a
real ``sqlite3.Error``:

- ``locked``: another connection holds ``BEGIN IMMEDIATE`` on a new ``runtime.db``, so
  the open's first write waits out the busy timeout and gets "database is locked".
- ``malformed``: ``runtime.db`` holds bytes that are not a SQLite database.
- ``read_only``: ``runtime.db`` is a read-only file, so the open's first write fails.

The run must end the way #256 ends an ``OSError`` from the same open. ``check_*``
functions are whole test bodies.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

SCENARIOS = ("locked", "malformed", "read_only")
TENANT = "tenant-b"
RUN_ID = "b-new"

_Failed = list[tuple[tuple[object, ...], dict[str, object]]]


def async_request(root: Path, tenant: str = TENANT, run_id: str = RUN_ID) -> Any:
    """A minimal start request for ``run_id`` under ``<root>/runs/<tenant>``."""

    from recertia.workers.run_worker import load_async_request

    tdir = root / "runs" / tenant
    req = load_async_request(
        {
            "run_id": run_id,
            "tenant_id": tenant,
            "request": run_id,
            "budget": {"max_attempts": 1},
            "workdir": str(root / "workspaces" / tenant / run_id),
            "skills_root": str(root / "skills"),
            "facts_root": str(root / "facts"),
            "runs_root": str(tdir),
            "index_path": str(tdir / "skill_index.db"),
        }
    )
    req.workdir.mkdir(parents=True, exist_ok=True)
    return req


def run_db(root: Path, tenant: str = TENANT) -> Path:
    return root / "runs" / tenant / "runtime.db"


@contextmanager
def sqlite_open_failure(root: Path, scenario: str, tenant: str = TENANT) -> Iterator[None]:
    """Make the next open of ``tenant``'s run database raise ``sqlite3.Error``."""

    db = run_db(root, tenant)
    db.parent.mkdir(parents=True, exist_ok=True)
    if scenario == "locked":
        holder = sqlite3.connect(db, isolation_level=None)
        try:
            holder.execute("BEGIN IMMEDIATE")
            yield
        finally:
            holder.rollback()
            holder.close()
    elif scenario == "malformed":
        db.write_bytes(b"this is not a SQLite database\n" * 64)
        yield
    elif scenario == "read_only":
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("root ignores a read-only file mode")
        db.touch()
        db.chmod(stat.S_IREAD)
        try:
            yield
        finally:
            db.chmod(stat.S_IREAD | stat.S_IWRITE)  # Windows cannot delete a read-only file
    else:
        raise ValueError(f"unknown scenario {scenario!r}")


def run_to_failure(root: Path, scenario: str) -> tuple[str, list[dict[str, Any]], _Failed]:
    """Run one start request against the broken run database; return outcome, events, on_failed calls."""

    from recertia.api.events import RunEventLog
    from recertia.workers.run_worker import AsyncRunWorker

    req = async_request(root)
    events = RunEventLog(root)
    failed: _Failed = []
    worker = AsyncRunWorker(events=events, on_failed=lambda *args, **kwargs: failed.append((args, kwargs)))
    try:
        with sqlite_open_failure(root, scenario):
            outcome = worker._run(req)
    finally:
        worker.shutdown(wait=True)
    return outcome, events.list_after(TENANT, RUN_ID), failed


def assert_ended_with_a_path_free_error(
    root: Path, outcome: str, log: list[dict[str, Any]], failed: _Failed
) -> None:
    """The #256 assertions: ``error`` then ``run.finished`` (``terminal: "error"``), one
    ``on_failed``, and no path in the event."""

    assert outcome == "failed"
    assert [e["type"] for e in log] == ["run.started", "error", "run.finished"], log
    error = log[1]["payload"]
    assert error["code"] == "run_failed" and error["retryable"] is False, error
    assert log[2]["payload"]["terminal"] == "error", log[2]
    assert failed == [((TENANT, RUN_ID), {"cancelled": False})], failed
    text = json.dumps(error)
    message = str(error["message"])
    assert message.startswith("cannot open the run's persistence ("), error
    for needle in (str(root), root.name, "runtime.db", ".lock"):
        assert needle not in text, (needle, text)
    assert "/" not in message and "\\" not in message, message


def check_sqlite_error_on_open_ends_the_run(tmp_path: Path, scenario: str) -> None:
    root = tmp_path / "host-only-dir"
    outcome, log, failed = run_to_failure(root, scenario)
    assert_ended_with_a_path_free_error(root, outcome, log, failed)
