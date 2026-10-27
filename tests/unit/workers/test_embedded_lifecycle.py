"""REC-029: EmbeddedQueueWorker start/stop transitions (review nits on #106).

ensure_started() while stop() is still waiting for an in-flight item must
not strand later work, and must not leave two threads polling.
stop() reports a join timeout.

Interleavings are forced with events, not sleeps: the in-flight item blocks
on ``release`` until the test lets it finish.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import recertia.workers.embedded as embedded_mod
from recertia.workers.embedded import EmbeddedQueueWorker
from recertia.workers.queue import QueueItem, RunQueue, open_run_queue


@dataclass
class _Req:
    run_id: str = ""
    tenant_id: str = ""
    cancel_check: Any = None


class _GatedWorker:
    """Stands in for AsyncRunWorker: 'gate' runs block until released."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.entered = threading.Event()
        self.ran: list[str] = []
        self.threads: set[int] = set()

    def _run(self, req: Any) -> str:
        self.threads.add(threading.get_ident())
        if req.run_id == "gate":
            self.entered.set()
            assert self.release.wait(10), "test never released the gated item"
        self.ran.append(req.run_id)
        return "succeeded"


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(
        embedded_mod,
        "load_async_request",
        lambda payload: _Req(),
    )
    queue = open_run_queue(tmp_path)
    worker = _GatedWorker()
    embedded = EmbeddedQueueWorker(worker, queue)  # type: ignore[arg-type]
    before = set(threading.enumerate())
    yield SimpleNamespace(queue=queue, worker=worker, embedded=embedded, before=before)
    worker.release.set()
    embedded._stop.set()  # teardown without relying on the code under test
    for t in set(threading.enumerate()) - before:
        if t.name == "recertia-queue":
            t.join(5)
    queue.close()


def _enqueue(queue: RunQueue, run_id: str) -> None:
    queue.enqueue(QueueItem(tenant_id="t1", run_id=run_id, kind="run", payload={}))


def _status(queue: RunQueue, run_id: str) -> str | None:
    row = queue.get("t1", run_id)
    return row.status if row else None


def _wait_terminal(queue: RunQueue, run_id: str, timeout: float = 5.0) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _status(queue, run_id)
        if status in {"succeeded", "failed", "cancelled"}:
            return status
        time.sleep(0.01)
    return _status(queue, run_id)


def _new_queue_threads(before: set[threading.Thread]) -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "recertia-queue" and t not in before]


def test_n1_ensure_started_during_stop_does_not_strand_work(rig: Any) -> None:
    rig.queue.set_max_in_flight("t1", 4)
    _enqueue(rig.queue, "gate")
    rig.embedded.ensure_started()
    assert rig.worker.entered.wait(5)  # the worker is inside the in-flight item

    # stop() has asked the thread to exit; the thread is still alive.
    rig.embedded.stop(timeout=0)
    # A create/resume enqueues and calls ensure_started() (app/runs.py) in this window.
    _enqueue(rig.queue, "after-stop")
    rig.embedded.ensure_started()
    rig.worker.release.set()

    assert _wait_terminal(rig.queue, "gate") == "succeeded"
    assert _wait_terminal(rig.queue, "after-stop") == "succeeded", "work enqueued after stop was stranded"
    assert rig.worker.ran == ["gate", "after-stop"]
    deadline = time.monotonic() + 5
    while len(_new_queue_threads(rig.before)) > 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(_new_queue_threads(rig.before)) == 1


def test_n1_repeated_ensure_started_during_stop_starts_one_thread(rig: Any) -> None:
    rig.queue.set_max_in_flight("t1", 4)
    _enqueue(rig.queue, "gate")
    rig.embedded.ensure_started()
    assert rig.worker.entered.wait(5)
    rig.embedded.stop(timeout=0)
    for _ in range(5):
        rig.embedded.ensure_started()
    # While the old thread is finishing, no second poller may exist.
    assert len(_new_queue_threads(rig.before)) == 1
    _enqueue(rig.queue, "after-stop")
    rig.worker.release.set()
    assert _wait_terminal(rig.queue, "after-stop") == "succeeded"
    deadline = time.monotonic() + 5
    while len(_new_queue_threads(rig.before)) > 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(_new_queue_threads(rig.before)) == 1


def test_n1_stop_after_ensure_started_wins(rig: Any) -> None:
    """stop() → ensure_started() → stop(): the last call wins, nothing keeps polling."""

    _enqueue(rig.queue, "gate")
    rig.embedded.ensure_started()
    assert rig.worker.entered.wait(5)
    rig.embedded.stop(timeout=0)
    rig.embedded.ensure_started()
    rig.worker.release.set()
    assert rig.embedded.stop(timeout=5) is True
    assert _new_queue_threads(rig.before) == []
    _enqueue(rig.queue, "after-final-stop")
    time.sleep(0.2)  # four poll intervals: nobody may claim it
    assert _status(rig.queue, "after-final-stop") == "queued"


def test_n2_stop_reports_join_timeout(rig: Any, caplog: pytest.LogCaptureFixture) -> None:
    _enqueue(rig.queue, "gate")
    rig.embedded.ensure_started()
    assert rig.worker.entered.wait(5)
    with caplog.at_level(logging.WARNING, logger=embedded_mod.__name__):
        assert rig.embedded.stop(timeout=0.05) is False
    assert any("did not stop" in r.getMessage() for r in caplog.records)
    rig.worker.release.set()
    assert rig.embedded.stop(timeout=5) is True
    assert _status(rig.queue, "gate") == "succeeded"  # in-flight item finished, not abandoned


def test_n2_stop_returns_true_when_idle_or_never_started(rig: Any) -> None:
    assert rig.embedded.stop() is True
    rig.embedded.ensure_started()
    assert rig.embedded.is_running()
    assert rig.embedded.stop() is True
    assert not rig.embedded.is_running()


def test_stop_all_stops_every_live_instance(tmp_path: Path) -> None:
    queues = [open_run_queue(tmp_path / str(i)) for i in range(3)]
    workers = [EmbeddedQueueWorker(None, q, worker_id=f"w{i}") for i, q in enumerate(queues)]  # type: ignore[arg-type]
    try:
        for w in workers:
            w.ensure_started()
        assert all(w.is_running() for w in workers)
        assert EmbeddedQueueWorker.stop_all(timeout=5) is True
        assert not any(w.is_running() for w in workers)
    finally:
        for w in workers:
            w._stop.set()
        for q in queues:
            q.close()


def test_teardown_fails_when_a_queue_worker_does_not_stop(rig: Any) -> None:
    """The suite teardown fails the test instead of leaking the thread."""

    from tests.conftest import stop_test_workers

    _enqueue(rig.queue, "gate")
    rig.embedded.ensure_started()
    assert rig.worker.entered.wait(5)
    with pytest.raises(pytest.fail.Exception, match="did not stop"):
        stop_test_workers(timeout=0.05)
    rig.worker.release.set()
    stop_test_workers(timeout=5)  # passes once the in-flight item ends
    assert _new_queue_threads(rig.before) == []


def test_teardown_joins_idle_async_pool_threads() -> None:
    """Idle ``recertia-run`` pool threads are joined, not left for a later fork."""

    from recertia.workers.run_worker import AsyncRunWorker
    from tests.conftest import stop_test_workers

    worker = AsyncRunWorker(events=None)  # type: ignore[arg-type]
    worker._pool.submit(lambda: None).result(5)
    pool_threads = [t for t in threading.enumerate() if t.name.startswith("recertia-run")]
    assert pool_threads
    stop_test_workers(timeout=5)
    assert not any(t.is_alive() for t in pool_threads)


class _NullEvents:
    def append(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs


def test_shutdown_all_is_bounded_when_a_run_hangs() -> None:
    """A hung run makes ``shutdown_all`` return False at its deadline, not block."""

    from recertia.workers.run_worker import AsyncRunWorker

    worker = AsyncRunWorker(events=None, max_workers=1)  # type: ignore[arg-type]
    release = threading.Event()
    entered = threading.Event()

    def hung() -> None:
        entered.set()
        release.wait(60)

    worker._pool.submit(hung)
    assert entered.wait(5)
    queued = worker._pool.submit(lambda: None)  # cancelled, never started
    pool_threads = [t for t in threading.enumerate() if t.name.startswith("recertia-run")]
    deadline = 0.5
    try:
        started = time.monotonic()
        assert AsyncRunWorker.shutdown_all(timeout=deadline) is False
        elapsed = time.monotonic() - started
        assert deadline * 0.9 <= elapsed < deadline + 2.0
        assert queued.cancelled()
    finally:
        release.set()
    for t in pool_threads:
        t.join(5)
    assert not any(t.is_alive() for t in pool_threads)
    assert AsyncRunWorker.shutdown_all(timeout=1) is True


def test_shutdown_prunes_the_registry_and_the_worker_is_reusable() -> None:
    """Shut-down workers leave ``_instances``; a reused worker gets a new pool."""

    from recertia.workers.run_worker import AsyncRunWorker

    workers = [AsyncRunWorker(events=_NullEvents()) for _ in range(20)]  # type: ignore[arg-type]
    assert all(w in AsyncRunWorker._instances for w in workers)
    assert AsyncRunWorker.shutdown_all(timeout=5) is True
    assert not any(w in AsyncRunWorker._instances for w in workers)
    workers[0].shutdown()  # idempotent, still unregistered
    assert workers[0] not in AsyncRunWorker._instances

    reused = workers[0]
    reused._run = lambda req: "succeeded"  # type: ignore[method-assign]
    reused.submit(SimpleNamespace(tenant_id="t1", run_id="r1"))  # type: ignore[arg-type]
    assert reused._futures[("t1", "r1")].result(5) == "succeeded"
    assert reused in AsyncRunWorker._instances
    assert AsyncRunWorker.shutdown_all(timeout=5) is True
    assert not any(w in AsyncRunWorker._instances for w in workers)


def test_shutdown_all_ignores_a_patched_time_monotonic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Teardown runs before monkeypatch undo; a test's fake clock must not break it."""

    from recertia.workers.run_worker import AsyncRunWorker

    worker = AsyncRunWorker(events=None)  # type: ignore[arg-type]
    worker._pool.submit(lambda: None).result(5)

    def exhausted_clock() -> float:
        raise StopIteration

    monkeypatch.setattr(time, "monotonic", exhausted_clock)
    assert AsyncRunWorker.shutdown_all(timeout=5) is True


def test_teardown_stops_queue_workers_before_pools(monkeypatch: pytest.MonkeyPatch) -> None:
    """A queue worker's last submit must not leak a pool past teardown.

    The stand-in ``stop_all`` submits a run, as a queue worker can while it
    stops. Queue first, then pools: ``shutdown_all`` closes the pool that
    submit used. Pools first: the submit restarts a closed pool after
    ``shutdown_all`` ran, so a ``recertia-run`` thread and a registered worker
    outlive teardown.
    """

    from recertia.workers.run_worker import AsyncRunWorker
    from tests.conftest import stop_test_workers_report

    worker = AsyncRunWorker(events=_NullEvents())  # type: ignore[arg-type]
    worker._run = lambda req: "succeeded"  # type: ignore[method-assign]
    before = {t for t in threading.enumerate() if t.name.startswith("recertia-run")}
    results: list[str] = []

    def last_submit_then_stop(cls: Any, timeout: float = 5.0) -> bool:
        del cls
        run_id = f"r{len(results)}"
        worker.submit(SimpleNamespace(tenant_id="t1", run_id=run_id))  # type: ignore[arg-type]
        results.append(worker._futures[("t1", run_id)].result(timeout))  # stop waits for in-flight work
        return True

    try:
        with monkeypatch.context() as mp:  # undone before this test's own teardown
            mp.setattr(EmbeddedQueueWorker, "stop_all", classmethod(last_submit_then_stop))
            assert stop_test_workers_report(timeout=5) is None
        assert results == ["succeeded"]
        run_threads = [t for t in threading.enumerate() if t.name.startswith("recertia-run")]
        leaked = [t for t in run_threads if t not in before and t.is_alive()]
        assert worker not in AsyncRunWorker._instances
        assert leaked == []
    finally:
        AsyncRunWorker.shutdown_all(timeout=5)
