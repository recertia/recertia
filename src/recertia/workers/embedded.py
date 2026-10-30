"""In-process drainer for the SQLite run queue.

Create and resume enqueue here. The request thread waits or returns 202; this
thread is the one that calls ``orchestrator.start``. ``recertia worker`` drains
the same table after an API restart.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import weakref
from dataclasses import replace
from typing import ClassVar, Literal

from recertia.persistence.root_lock import RootBusy
from recertia.workers.queue import ROOT_BUSY_RETRY_S, Claim, RunQueue
from recertia.workers.run_worker import AsyncRunWorker, load_async_request

_log = logging.getLogger(__name__)

_State = Literal["stopped", "running", "stopping"]


class EmbeddedQueueWorker:
    """One daemon thread. Claim, execute, finish.

    Lifecycle, every transition under ``_guard``:

    - ``stopped``: no thread. ``ensure_started`` starts one -> ``running``.
    - ``running``: one thread polls. ``stop`` -> ``stopping``.
    - ``stopping``: the thread was asked to exit and may still be finishing its
      in-flight item. It never claims again. ``ensure_started`` here does not
      start a second thread; it asks the exiting thread to hand over to exactly
      one fresh thread when it leaves its loop (so work enqueued meanwhile is
      not stranded). A later ``stop`` cancels that hand-over.

    Each thread has its own stop event, so a hand-over can never revive or be
    confused with the thread that is exiting.
    """

    _instances: ClassVar[weakref.WeakSet[EmbeddedQueueWorker]] = weakref.WeakSet()

    def __init__(self, worker: AsyncRunWorker, queue: RunQueue, *, worker_id: str = "embedded") -> None:
        self.worker = worker
        self.queue = queue
        self.worker_id = worker_id
        self._stop = threading.Event()  # the current thread's event
        self._guard = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state: _State = "stopped"
        self._restart = False
        self._busy = False
        EmbeddedQueueWorker._instances.add(self)

    def ensure_started(self) -> None:
        with self._guard:
            if self._thread is not None and not self._thread.is_alive():
                self._state = "stopped"  # defensive: the thread died without a hand-over
            if self._state == "running":
                return
            if self._state == "stopping":
                self._restart = True
                return
            self._spawn()

    def is_running(self) -> bool:
        """True while a thread is polling (not stopped and not stopping)."""

        with self._guard:
            return self._state == "running"

    def stop(self, timeout: float = 5.0) -> bool:
        """Stop polling and wait up to ``timeout`` seconds for the thread to exit.

        An in-flight item is finished, never abandoned, but only items that end
        within ``timeout`` are waited for. Returns True when no worker thread
        of this instance is left alive, False (and logs a warning) when the
        join timed out; the thread then exits on its own after its item.
        ``ensure_started`` starts polling again later.
        """

        with self._guard:
            thread = self._thread
            self._restart = False
            if self._state == "running":
                self._state = "stopping"
                self._stop.set()
        if thread is None:
            return True
        if thread is threading.current_thread():
            return False
        thread.join(timeout)
        if thread.is_alive():
            _log.warning(
                "embedded queue worker did not stop within %.3gs; it exits after its in-flight item",
                timeout,
            )
            return False
        return True

    @classmethod
    def stop_all(cls, timeout: float = 5.0) -> bool:
        """``stop`` every live instance (process or test teardown). True if all stopped."""

        results = [w.stop(timeout) for w in list(cls._instances)]
        return all(results)

    def _spawn(self) -> None:
        """Start one polling thread. Caller holds ``_guard``."""

        self._stop = threading.Event()
        self._restart = False
        self.queue.register_worker(
            self.worker_id,
            capacity=1,
            hostname=_hostname(),
            pid=os.getpid(),
        )
        thread = threading.Thread(target=self._loop, args=(self._stop,), name="recertia-queue", daemon=True)
        self._thread = thread
        self._state = "running"
        thread.start()

    def flush(self, timeout: float = 30.0) -> None:
        """Block until queued and in-flight items have a terminal status."""

        self.ensure_started()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._busy and self.queue.outstanding() == 0:
                return
            time.sleep(0.05)
        raise TimeoutError("embedded queue worker did not drain")

    def _loop(self, stop: threading.Event) -> None:
        try:
            while not stop.is_set():
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
        finally:
            self._exited(threading.current_thread())

    def _exited(self, thread: threading.Thread) -> None:
        with self._guard:
            if self._thread is not thread:
                return
            if self._state == "stopping" and self._restart:
                try:
                    self._spawn()  # hand over: this thread claims nothing more
                    return
                except Exception:  # noqa: BLE001 - leave a clean 'stopped' state
                    _log.exception("embedded queue worker hand-over failed")
            self._thread = None
            self._state = "stopped"
            self._restart = False

    def _execute(self, claim: Claim) -> None:
        self.queue.mark_running(claim)
        req = load_async_request(claim.payload)
        if not req.run_id:
            req = replace(req, run_id=claim.run_id, tenant_id=claim.tenant_id)
        req.cancel_check = lambda: self.queue.cancel_requested(claim.tenant_id, claim.run_id)
        try:
            outcome = self.worker._run(req)
        except RootBusy:
            # Retryable (REC-058): back to 'queued' without spending an attempt.
            self.queue.requeue(claim, delay_s=ROOT_BUSY_RETRY_S, error_code="root_busy")
            return
        except Exception:
            # The run worker could not end the run itself (for example, writing its
            # terminal events failed). Keep the cause for the operator.
            _log.exception(
                "run %s/%s: the run worker raised; the run is marked failed", claim.tenant_id, claim.run_id
            )
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
