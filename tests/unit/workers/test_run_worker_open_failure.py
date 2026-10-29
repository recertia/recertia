"""What ``AsyncRunWorker._run`` does when opening the run's persistence fails.

``build_default_orchestrator`` runs before the ``try`` that holds ``except Exception``.
``RootBusy`` is recorded as retryable and re-raised for the queue to requeue. Any other
``OSError`` from that open, such as a lock call that fails with an errno that is not a
busy refusal, ends the run: an ``error`` event (code ``run_failed``, no filesystem path),
``run.finished`` with ``terminal: "error"``, and one ``on_failed`` call.

This replaces an earlier pin that asserted the opposite (the ``OSError`` escaped ``_run``
with no terminal event and no ``on_failed`` call, so the run stayed ``queued``).
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
