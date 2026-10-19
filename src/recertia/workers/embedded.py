"""In-process drainer for the SQLite run queue.

Create and resume enqueue here. The request thread waits or returns 202; this
thread is the one that calls ``orchestrator.start``. ``recertia worker`` drains
the same table after an API restart.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import replace

from recertia.workers.queue import Claim, RunQueue
from recertia.workers.run_worker import AsyncRunWorker, load_async_request


class EmbeddedQueueWorker:
    """One daemon thread. Claim, execute, finish."""

    def __init__(self, worker: AsyncRunWorker, queue: RunQueue, *, worker_id: str = "embedded") -> None:
        self.worker = worker
        self.queue = queue
        self.worker_id = worker_id
        self._stop = threading.Event()
        self._guard = threading.Lock()
        self._thread: threading.Thread | None = None
        self._busy = False

    def ensure_started(self) -> None:
        with self._guard:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self.queue.register_worker(
                self.worker_id,
                capacity=1,
                hostname=_hostname(),
                pid=os.getpid(),
            )
            self._thread = threading.Thread(
                target=self._loop, name="recertia-queue", daemon=True
            )
            self._thread.start()

    def flush(self, timeout: float = 30.0) -> None:
        """Block until queued and in-flight items have a terminal status."""

        self.ensure_started()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._busy and self.queue.outstanding() == 0:
                return
            time.sleep(0.05)
        raise TimeoutError("embedded queue worker did not drain")

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.queue.heartbeat_worker(self.worker_id)
            claims = self.queue.claim(self.worker_id, limit=1)
            if not claims:
                time.sleep(0.05)
                continue
            self._busy = True
            try:
                self._execute(claims[0])
            finally:
                self._busy = False

    def _execute(self, claim: Claim) -> None:
        self.queue.mark_running(claim)
        req = load_async_request(claim.payload)
        if not req.run_id:
            req = replace(req, run_id=claim.run_id, tenant_id=claim.tenant_id)
        req.cancel_check = lambda: self.queue.cancel_requested(claim.tenant_id, claim.run_id)
        try:
            outcome = self.worker._run(req)
        except Exception:
            self.queue.finish(claim, status="failed", error_code="run_failed")
            return
        if outcome == "cancelled":
            self.queue.finish(claim, status="cancelled")
        elif outcome == "run_busy":
            self.queue.finish(claim, status="failed", error_code="run_busy")
        elif outcome == "workspace_busy":
            self.queue.finish(claim, status="failed", error_code="workspace_busy")
        elif outcome == "failed":
            self.queue.finish(claim, status="failed", error_code="run_failed")
        else:
            self.queue.finish(claim, status="succeeded")


def _hostname() -> str:
    uname = getattr(os, "uname", None)
    if uname is None:
        return "localhost"
    return str(uname().nodename)
