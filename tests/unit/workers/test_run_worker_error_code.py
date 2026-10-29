"""A run that ends on an exception: the ``error`` event's code.

A ``RecertiaError`` keeps its own code (for example ``authoring_prior_missing`` or
``execution_unavailable``); any other exception gives ``run_failed``. Either way the event is
not retryable and the run ends with ``terminal: "error"``. The queue record's ``error_code``
stays ``run_failed``.

``RootBusy`` and ``WorkspaceBusy`` are left out: the worker handles them before this path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from recertia.api.events import RunEventLog
from recertia.app.errors import ServiceError
from recertia.distill.prior import AuthoringPriorMissing
from recertia.errors import (
    Busy,
    ConfigError,
    Conflict,
    ExecutionUnavailable,
    Forbidden,
    InvalidTenantId,
    NotFound,
    QuotaExceeded,
    RateLimited,
    RecertiaError,
    SecretUnavailable,
    Unauthorized,
)
from recertia.ops.receipts import ReceiptError
from recertia.persistence.db import MigrationChecksumError
from recertia.runtime.errors import (
    InvalidCursor,
    LeaseLost,
    LedgerLockTimeout,
    LedgerUnavailable,
    RunIdExists,
    StateCorrupt,
)
from recertia.solver.model_allowlist import ModelAllowlistError
from recertia.solver.resilience import ModelUnavailable
from recertia.solver.sandbox import SandboxError
from recertia.workers import run_worker
from recertia.workers.run_worker import AsyncRunWorker
from tests.unit.persistence.test_root_lock_writers import _async_request

# Every RecertiaError class that can reach the worker's generic handler, with its code.
_TYPED: list[tuple[type[RecertiaError], str]] = [
    (AuthoringPriorMissing, "authoring_prior_missing"),
    (Busy, "busy"),
    (Conflict, "conflict"),
    (ExecutionUnavailable, "execution_unavailable"),
    (ModelUnavailable, "execution_unavailable"),
    (SandboxError, "execution_unavailable"),
    (Forbidden, "forbidden"),
    (RecertiaError, "internal_error"),
    (ServiceError, "internal_error"),
    (ConfigError, "invalid_config"),
    (InvalidCursor, "invalid_cursor"),
    (InvalidTenantId, "invalid_input"),
    (LeaseLost, "lease_lost"),
    (LedgerUnavailable, "ledger_unavailable"),
    (LedgerLockTimeout, "lock_timeout"),
    (NotFound, "not_found"),
    (QuotaExceeded, "quota_exceeded"),
    (RateLimited, "rate_limited"),
    (ReceiptError, "receipt_error"),
    (RunIdExists, "run_id_exists"),
    (SecretUnavailable, "secret_unavailable"),
    (StateCorrupt, "state_corrupt"),
    (MigrationChecksumError, "state_corrupt"),
    (Unauthorized, "unauthorized"),
]

# Plain exceptions, including one that carries its own ``code`` attribute.
_PLAIN: list[Exception] = [
    RuntimeError("boom"),
    ValueError("bad value"),
    ModelAllowlistError("model not on the allowlist"),
]


def _raise_from_start(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    real = run_worker.build_default_orchestrator

    def build(*args: Any, **kwargs: Any) -> Any:
        bundle = real(*args, **kwargs)

        def start(*_a: Any, **_k: Any) -> None:
            raise exc

        monkeypatch.setattr(bundle.orchestrator, "start", start)
        return bundle

    monkeypatch.setattr(run_worker, "build_default_orchestrator", build)


def _run_failing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> list[dict[str, Any]]:
    _raise_from_start(monkeypatch, exc)
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
    assert log[2]["payload"]["terminal"] == "error", log[2]
    return log


@pytest.mark.parametrize(("cls", "code"), _TYPED, ids=[f"{c.__name__}-{code}" for c, code in _TYPED])
def test_a_recertia_error_keeps_its_code_in_the_error_event(
    cls: type[RecertiaError], code: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exc = cls(f"{cls.__name__} stopped the run")
    log = _run_failing(tmp_path, monkeypatch, exc)
    assert log[1]["payload"] == {"code": code, "message": str(exc), "retryable": False}, log[1]


@pytest.mark.parametrize("exc", _PLAIN, ids=[type(e).__name__ for e in _PLAIN])
def test_any_other_exception_gives_run_failed(
    exc: Exception, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = _run_failing(tmp_path, monkeypatch, exc)
    assert log[1]["payload"] == {"code": "run_failed", "message": str(exc), "retryable": False}, log[1]


def test_the_queue_record_still_says_run_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fastapi")
    from recertia.api import create_app
    from recertia.workers.queue import QueueItem
    from recertia.workers.run_worker import dump_async_request

    _raise_from_start(monkeypatch, ExecutionUnavailable("no sandbox"))
    root = tmp_path / "root"
    state = create_app(root=root).state.recertia
    req = _async_request(root, "tenant-a", "r-queue")
    state.queue.enqueue(
        QueueItem(tenant_id="tenant-a", run_id="r-queue", kind="run", payload=dump_async_request(req))  # type: ignore[arg-type]
    )
    claims = state.queue.claim(state.embedded.worker_id, limit=1)
    assert len(claims) == 1
    state.embedded._execute(claims[0])
    row = state.queue.get("tenant-a", "r-queue")
    assert (row.status, row.error_code) == ("failed", "run_failed")
    errors = [
        e["payload"]["code"]
        for e in RunEventLog(root).list_after("tenant-a", "r-queue")
        if e["type"] == "error"
    ]
    assert errors == ["execution_unavailable"]
