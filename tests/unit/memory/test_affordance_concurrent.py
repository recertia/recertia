"""REL-5 F19: concurrent affordance increments do not drop counts."""

from __future__ import annotations

from pathlib import Path
from threading import Thread

from recertia.memory.affordance import AffordanceStore
from recertia.solver.tools import ToolResult


def test_two_attempts_do_not_lose_increments(tmp_path: Path) -> None:
    path = tmp_path / "affordances.json"
    result = ToolResult(tool="echo", ok=True, duration_s=0.01, cost_usd=0.0)

    def worker() -> None:
        store = AffordanceStore(path)
        try:
            store.record_tool(result)
        finally:
            store.close()

    threads = [Thread(target=worker) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    store = AffordanceStore(path)
    try:
        agg = store.tool("echo")
        assert agg is not None
        assert agg.invocations == 50
    finally:
        store.close()
