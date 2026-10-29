"""A run that ends on an exception: the ``error`` event's code.

A ``RecertiaError`` keeps its own code (for example ``authoring_prior_missing``); any other
exception gives ``run_failed``. Either way the event is not retryable and the run ends with
``terminal: "error"``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from recertia.api.events import RunEventLog
from recertia.distill.prior import AuthoringPriorMissing
from recertia.workers import run_worker
from recertia.workers.run_worker import AsyncRunWorker
from tests.unit.persistence.test_root_lock_writers import _async_request


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (
            AuthoringPriorMissing("default authoring prior policy/authoring-prior.json not found"),
            "authoring_prior_missing",
        ),
        (RuntimeError("boom"), "run_failed"),
    ],
)
def test_the_error_event_carries_a_recertia_error_code(
    exc: Exception, code: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = run_worker.build_default_orchestrator

    def build(*args: Any, **kwargs: Any) -> Any:
        bundle = real(*args, **kwargs)

        def start(*_a: Any, **_k: Any) -> None:
            raise exc

        monkeypatch.setattr(bundle.orchestrator, "start", start)
        return bundle

    monkeypatch.setattr(run_worker, "build_default_orchestrator", build)
    req = _async_request(tmp_path, "tenant-a", "r-code")
    events = RunEventLog(tmp_path)
    worker = AsyncRunWorker(events=events)
    try:
        outcome = worker._run(req)  # type: ignore[arg-type]
    finally:
        worker.shutdown(wait=True)
    log = events.list_after("tenant-a", "r-code")
    assert outcome == "failed"
    assert [e["type"] for e in log] == ["run.started", "error", "run.finished"], log
    assert log[1]["payload"] == {"code": code, "message": str(exc), "retryable": False}, log[1]
    assert log[2]["payload"]["terminal"] == "error", log[2]
