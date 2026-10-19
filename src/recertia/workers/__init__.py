"""Background workers for the durable run queue.

``AsyncRunWorker`` loads on attribute access. Importing this package must not
import FastAPI: ``recertia worker`` and ``recertia --help`` only need
``workers.serve``, and FastAPI is an optional extra.
"""

from __future__ import annotations

from typing import Any

__all__ = ["AsyncRunWorker"]


def __getattr__(name: str) -> Any:
    if name == "AsyncRunWorker":
        from recertia.workers.run_worker import AsyncRunWorker

        globals()["AsyncRunWorker"] = AsyncRunWorker
        return AsyncRunWorker
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
