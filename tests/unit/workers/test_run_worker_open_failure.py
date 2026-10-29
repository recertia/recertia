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
