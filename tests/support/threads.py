"""Start test threads together, so the work they race on overlaps."""

from __future__ import annotations

import threading
from collections.abc import Callable


def run_together(workers: list[Callable[[], None]]) -> None:
    """Run each worker on its own thread, all released by one barrier, and join them.

    A thread still alive after 60 s fails the test instead of hanging the run.
    """

    barrier = threading.Barrier(len(workers))

    def start(work: Callable[[], None]) -> None:
        barrier.wait()
        work()

    threads = [threading.Thread(target=start, args=(work,)) for work in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads)
