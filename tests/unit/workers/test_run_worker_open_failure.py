"""Pins what ``AsyncRunWorker._run`` does when opening the run's persistence fails.

``build_default_orchestrator`` runs before the ``try`` that holds ``except Exception``,
so only ``RootBusy`` is handled there (recorded as retryable, then re-raised for the
queue to requeue). Any other failure, such as a lock call that fails with an ``OSError``
that is not a busy refusal, escapes ``_run`` unchanged: no ``error`` or ``run.finished``
event and no ``on_failed`` call. The queue worker, not ``_run``, decides what happens to
the claim next. This test pins that current behaviour; it does not endorse it.
"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from recertia.api.events import RunEventLog
from recertia.persistence.layout import RuntimeLayout
from recertia.persistence.root_lock import RootBusy, RootLock
from recertia.workers.run_worker import AsyncRunWorker
from tests.unit.persistence.test_root_lock_writers import _async_request


def test_non_busy_lock_failure_escapes_run_with_no_terminal_event_and_no_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def lock_fails(self: RootLock, *, timeout_s: float | None = None) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(RootLock, "acquire_shared", lock_fails)
    root = tmp_path / "rt"
    req = _async_request(root, "tenant-b", "b-new")
    events = RunEventLog(root)
    failed: list[tuple[object, ...]] = []
    worker = AsyncRunWorker(events=events, on_failed=lambda *args, **kwargs: failed.append(args))
    try:
        with pytest.raises(OSError) as info:
            worker._run(req)  # type: ignore[arg-type]
        assert info.value.errno == errno.ENOSPC
        log = events.list_after("tenant-b", "b-new")
        assert [e["type"] for e in log] == ["run.started"], log
        assert failed == []
    finally:
        worker.shutdown(wait=True)


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
