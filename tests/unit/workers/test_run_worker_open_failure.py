"""What ``AsyncRunWorker._run`` does when opening the run's persistence fails.

``build_default_orchestrator`` runs before the ``try`` that holds ``except Exception``.
``RootBusy`` is recorded as retryable and re-raised for the queue to requeue. Any other
``OSError`` from that open, such as a lock call that fails with an errno that is not a
busy refusal, ends the run: an ``error`` event (code ``run_failed``, no filesystem path),
``run.finished`` with ``terminal: "error"``, and one ``on_failed`` call.

This replaces an earlier pin that asserted the opposite (the ``OSError`` escaped ``_run``
with no terminal event and no ``on_failed`` call, so the run stayed ``queued``).

REC-096: an ``sqlite3.Error`` from the same open (a locked, malformed or read-only run
database) ends the run the same way; any other exception still escapes ``_run``.
REC-104 pins the order of the terminal writes, the server log on the event-write failure
path (through the embedded queue worker), and the run record over the public API.
"""

from __future__ import annotations

import errno
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from recertia.api.events import RunEventLog
from recertia.persistence.layout import RuntimeLayout
from recertia.persistence.root_lock import RootBusy, RootLock
from recertia.workers.run_worker import AsyncRunWorker
from tests.support.sqlite_open_failures import SCENARIOS, check_sqlite_error_on_open_ends_the_run
from tests.unit.persistence.test_root_lock_writers import _async_request

_Failed = list[tuple[tuple[object, ...], dict[str, object]]]


def _run_to_failure(root: Path) -> tuple[str, list[dict[str, Any]], _Failed]:
    req = _async_request(root, "tenant-b", "b-new")
    events = RunEventLog(root)
    failed: _Failed = []
    worker = AsyncRunWorker(events=events, on_failed=lambda *args, **kwargs: failed.append((args, kwargs)))
    try:
        outcome = worker._run(req)  # type: ignore[arg-type]
    finally:
        worker.shutdown(wait=True)
    return outcome, events.list_after("tenant-b", "b-new"), failed


def _assert_terminal_and_path_free(
    root: Path,
    outcome: str,
    log: list[dict[str, Any]],
    failed: _Failed,
) -> None:
    assert outcome == "failed"
    assert [e["type"] for e in log] == ["run.started", "error", "run.finished"], log
    error = log[1]["payload"]
    assert error["code"] == "run_failed" and error["retryable"] is False, error
    assert log[2]["payload"]["terminal"] == "error", log[2]
    assert failed == [(("tenant-b", "b-new"), {"cancelled": False})], failed
    text = json.dumps(error)
    message = str(error["message"])
    assert message, error
    for needle in (str(root), root.name, ".lock", str(RuntimeLayout(root).root_lock)):
        assert needle not in text, (needle, text)
    assert "/" not in message and "\\" not in message, message


def test_lock_path_that_is_a_directory_ends_the_run_with_a_path_free_error(tmp_path: Path) -> None:
    """A real non-busy lock failure: the root lock path is a directory, so opening it
    fails with an ``OSError`` (``IsADirectoryError`` on POSIX)."""

    root = tmp_path / "host-only-dir"
    lock_path = RuntimeLayout(root).root_lock
    lock_path.mkdir(parents=True)
    outcome, log, failed = _run_to_failure(root)
    _assert_terminal_and_path_free(root, outcome, log, failed)


def test_non_busy_lock_errno_ends_the_run_with_a_path_free_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flipped from the earlier pin: the same ``ENOSPC`` lock failure, now terminal."""

    def lock_fails(self: RootLock, *, timeout_s: float | None = None) -> None:
        raise OSError(errno.ENOSPC, "No space left on device", str(self.path))

    monkeypatch.setattr(RootLock, "acquire_shared", lock_fails)
    root = tmp_path / "host-only-dir"
    outcome, log, failed = _run_to_failure(root)
    _assert_terminal_and_path_free(root, outcome, log, failed)


def test_root_busy_event_message_carries_no_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``root_busy`` run event is the text a client sees: it names no lock or root path."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0")
    root = tmp_path / "host-only-dir"
    req = _async_request(root, "tenant-b", "b-new")
    events = RunEventLog(root)
    worker = AsyncRunWorker(events=events)
    holder = RootLock(RuntimeLayout(root).root_lock)
    holder.try_exclusive()
    try:
        with pytest.raises(RootBusy):
            worker._run(req)  # type: ignore[arg-type]
    finally:
        holder.release()
        worker.shutdown(wait=True)
    errors = [e["payload"] for e in events.list_after("tenant-b", "b-new") if e["type"] == "error"]
    assert [p["code"] for p in errors] == ["root_busy"], errors
    message = str(errors[0]["message"])
    assert message == "cannot open the runtime root while the root lock is held"
    assert str(tmp_path) not in message and ".lock" not in message


def _open_failure_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == "recertia.workers.run_worker" and "opening run persistence failed" in r.getMessage()
    ]


def test_open_failure_logs_the_full_error_on_the_server_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The operator gets the exception, path included, in the server log; the event does not."""

    root = tmp_path / "host-only-dir"
    lock_path = RuntimeLayout(root).root_lock
    lock_path.mkdir(parents=True)
    with caplog.at_level(logging.ERROR, logger="recertia.workers.run_worker"):
        outcome, log, failed = _run_to_failure(root)
    _assert_terminal_and_path_free(root, outcome, log, failed)
    records = _open_failure_records(caplog)
    assert len(records) == 1, caplog.records
    record = records[0]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None and isinstance(record.exc_info[1], OSError), record.exc_info
    assert "tenant-b/b-new" in record.getMessage()
    assert str(lock_path) in logging.Formatter().format(record)


def test_open_failure_through_the_api_keeps_paths_out_of_every_response(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """End to end on the embedded queue worker: the queue row ends ``failed``, the error is
    logged, and neither the backlog nor the follow response names the root or the lock path."""

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from recertia.api import create_app
    from recertia.workers.queue import QueueItem
    from recertia.workers.run_worker import dump_async_request

    root = tmp_path / "host-only-dir"
    app = create_app(root=root)
    key = app.state.api_keys.issue(tenant_id="tenant-b", scopes={"runs"}, actor="test").secret
    state = app.state.recertia
    req = _async_request(root, "tenant-b", "b-new")
    state.queue.enqueue(
        QueueItem(tenant_id="tenant-b", run_id="b-new", kind="run", payload=dump_async_request(req))  # type: ignore[arg-type]
    )
    lock_path = RuntimeLayout(root).root_lock
    lock_path.unlink(missing_ok=True)
    lock_path.mkdir(parents=True)
    claims = state.queue.claim(state.embedded.worker_id, limit=1)
    assert len(claims) == 1
    with caplog.at_level(logging.ERROR, logger="recertia.workers.run_worker"):
        state.embedded._execute(claims[0])
    row = state.queue.get("tenant-b", "b-new")
    assert (row.status, row.error_code) == ("failed", "run_failed")
    assert len(_open_failure_records(caplog)) == 1
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-API-Key": key}
    bodies = {
        "backlog": client.get("/v1/runs/b-new/events", headers=headers),
        "follow": client.get("/v1/runs/b-new/events?follow=1", headers=headers),
    }
    for name, resp in bodies.items():
        assert resp.status_code == 200, (name, resp.text)
        assert "run_failed" in resp.text and "run.finished" in resp.text, (name, resp.text)
        for needle in (str(root), root.name, ".lock"):
            assert needle not in resp.text, (name, needle)


def test_open_failure_still_fails_the_run_when_writing_the_events_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a full disk the event log may fail too: ``on_failed`` must still end the run record."""

    root = tmp_path / "host-only-dir"
    RuntimeLayout(root).root_lock.mkdir(parents=True)
    real_append = RunEventLog.append

    def append(self: RunEventLog, tenant_id: str, run_id: str, kind: str, *args: Any, **kwargs: Any) -> Any:
        if kind == "error":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_append(self, tenant_id, run_id, kind, *args, **kwargs)

    monkeypatch.setattr(RunEventLog, "append", append)
    req = _async_request(root, "tenant-b", "b-new")
    failed: _Failed = []
    worker = AsyncRunWorker(events=RunEventLog(root), on_failed=lambda *a, **k: failed.append((a, k)))
    try:
        with pytest.raises(OSError) as raised:
            worker._run(req)  # type: ignore[arg-type]
    finally:
        worker.shutdown(wait=True)
    assert raised.value.errno == errno.ENOSPC
    assert failed == [(("tenant-b", "b-new"), {"cancelled": False})], failed


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_sqlite_error_on_open_ends_the_run_with_a_path_free_error(tmp_path: Path, scenario: str) -> None:
    """REC-096: a locked, malformed or read-only run database used to raise out of
    ``_run`` and leave the run ``queued``; now it ends like the ``OSError`` case."""

    check_sqlite_error_on_open_ends_the_run(tmp_path, scenario)


def test_other_errors_on_open_still_escape_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The catch is narrow: an error that is neither ``OSError`` nor ``sqlite3.Error``
    is not reported as an open failure."""

    import recertia.workers.run_worker as run_worker

    def build_fails(*args: object, **kwargs: object) -> object:
        raise RuntimeError("wiring bug")

    monkeypatch.setattr(run_worker, "build_default_orchestrator", build_fails)
    root = tmp_path / "host-only-dir"
    req = _async_request(root, "tenant-b", "b-new")
    events = RunEventLog(root)
    failed: _Failed = []
    worker = AsyncRunWorker(events=events, on_failed=lambda *args, **kwargs: failed.append((args, kwargs)))
    try:
        with pytest.raises(RuntimeError, match="wiring bug"):
            worker._run(req)  # type: ignore[arg-type]
    finally:
        worker.shutdown(wait=True)
    assert [e["type"] for e in events.list_after("tenant-b", "b-new")] == ["run.started"]
    assert failed == []


def test_open_failure_ends_the_run_record_after_both_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``on_failed`` runs after the ``error`` and ``run.finished`` writes, so a reader that
    sees the record end finds the terminal events already in the log."""

    calls: list[str] = []
    real_append = RunEventLog.append

    def append(self: RunEventLog, tenant_id: str, run_id: str, kind: str, *args: Any, **kwargs: Any) -> Any:
        calls.append(kind)
        return real_append(self, tenant_id, run_id, kind, *args, **kwargs)

    monkeypatch.setattr(RunEventLog, "append", append)
    root = tmp_path / "host-only-dir"
    RuntimeLayout(root).root_lock.mkdir(parents=True)
    req = _async_request(root, "tenant-b", "b-new")
    worker = AsyncRunWorker(
        events=RunEventLog(root), on_failed=lambda *args, **kwargs: calls.append("on_failed")
    )
    try:
        assert worker._run(req) == "failed"  # type: ignore[arg-type]
    finally:
        worker.shutdown(wait=True)
    assert calls == ["run.started", "error", "run.finished", "on_failed"], calls


def test_event_write_failure_after_an_open_failure_is_logged_by_the_queue_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Through ``EmbeddedQueueWorker._execute``: the open failure is logged by the run
    worker, the event-write ``OSError`` that then escapes ``_run`` is logged by the queue
    worker, and the queue row still ends ``failed``. No event names a path."""

    from recertia.workers.embedded import EmbeddedQueueWorker
    from recertia.workers.queue import QueueItem, open_run_queue
    from recertia.workers.run_worker import dump_async_request

    root = tmp_path / "host-only-dir"
    RuntimeLayout(root).root_lock.mkdir(parents=True)
    req = _async_request(root, "tenant-b", "b-new")
    real_append = RunEventLog.append

    def append(self: RunEventLog, tenant_id: str, run_id: str, kind: str, *args: Any, **kwargs: Any) -> Any:
        if kind == "error":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_append(self, tenant_id, run_id, kind, *args, **kwargs)

    monkeypatch.setattr(RunEventLog, "append", append)
    events = RunEventLog(root)
    worker = AsyncRunWorker(events=events)
    queue = open_run_queue(root)
    embedded = EmbeddedQueueWorker(worker, queue, worker_id="embedded-test")
    try:
        queue.enqueue(
            QueueItem(tenant_id="tenant-b", run_id="b-new", kind="run", payload=dump_async_request(req))  # type: ignore[arg-type]
        )
        claims = queue.claim("embedded-test", limit=1)
        assert len(claims) == 1
        with caplog.at_level(logging.ERROR):
            embedded._execute(claims[0])
        row = queue.get("tenant-b", "b-new")
        assert row is not None
        assert (row.status, row.error_code) == ("failed", "run_failed")
    finally:
        queue.close()
        worker.shutdown(wait=True)
    opened = _open_failure_records(caplog)
    assert len(opened) == 1, caplog.records
    assert opened[0].levelno == logging.ERROR
    assert opened[0].exc_info is not None and isinstance(
        opened[0].exc_info[1], IsADirectoryError | PermissionError
    )
    queue_records = [r for r in caplog.records if r.name == "recertia.workers.embedded"]
    assert len(queue_records) == 1, caplog.records
    record = queue_records[0]
    assert record.levelno == logging.ERROR
    assert "tenant-b/b-new" in record.getMessage()
    assert record.exc_info is not None, record
    written = record.exc_info[1]
    assert isinstance(written, OSError) and written.errno == errno.ENOSPC, record.exc_info
    log = events.list_after("tenant-b", "b-new")
    assert [e["type"] for e in log] == ["run.started"], log
    text = json.dumps(log)
    for needle in (str(root), root.name, ".lock"):
        assert needle not in text, (needle, text)


def test_open_failure_over_the_api_moves_the_run_record_from_queued_to_error(tmp_path: Path) -> None:
    """Public routes only: ``POST /v1/runs`` (async) answers ``queued``; the embedded
    queue worker's open of the run's skill index then fails with ``sqlite3.DatabaseError``,
    and ``GET /v1/runs/{run_id}`` reports ``error``. Before REC-096 it stayed ``queued``."""

    pytest.importorskip("fastapi")
    import time

    from fastapi.testclient import TestClient

    from recertia.api import create_app

    root = tmp_path / "host-only-dir"
    app = create_app(root=root)
    key = app.state.api_keys.issue(tenant_id="tenant-b", scopes={"runs"}, actor="test").secret
    index = root / "runs" / "tenant-b" / "skill_index.db"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_bytes(b"this is not a SQLite database\n" * 64)
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-API-Key": key}
    created = client.post(
        "/v1/runs", json={"request": "x", "mode": "async", "run_id": "b-new"}, headers=headers
    )
    assert created.status_code == 202, created.text
    assert created.json()["status"] == "queued", created.json()
    seen = ["queued"]
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        resp = client.get("/v1/runs/b-new", headers=headers)
        assert resp.status_code == 200, resp.text
        status = resp.json()["status"]
        if status != seen[-1]:
            seen.append(status)
        if status not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert seen[0] == "queued" and seen[-1] == "error", seen
