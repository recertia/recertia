"""Run admission, lookup, and listing. Routes do not call the quota store."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Protocol


class RunQuota(Protocol):
    def admit(self, tenant_id: str, *, quota: Any = None) -> None: ...

    def release_inflight(self, tenant_id: str) -> None: ...

    def complete(self, tenant_id: str, *, cost_usd: float = 0.0) -> None: ...


class RunService:
    """create/resume admit here. get and list read the same cache the routes use."""

    def __init__(
        self,
        quota: RunQuota,
        runs: dict[tuple[str, str], Any],
        load_run: Callable[[str, str], Any | None],
    ) -> None:
        self.quota = quota
        self.runs = runs
        self.load_run = load_run

    def admit(self, tenant_id: str) -> None:
        self.quota.admit(tenant_id)

    def release(self, tenant_id: str) -> None:
        self.quota.release_inflight(tenant_id)

    def complete(self, tenant_id: str, *, cost_usd: float = 0.0) -> None:
        self.quota.complete(tenant_id, cost_usd=cost_usd)

    def get(self, tenant_id: str, run_id: str) -> Any | None:
        key = (tenant_id, run_id)
        cached = self.runs.get(key)
        if cached is not None:
            return cached
        loaded = self.load_run(tenant_id, run_id)
        if loaded is not None:
            self.runs[key] = loaded
        return loaded

    def list(
        self,
        tenant_id: str,
        *,
        runs_root: Path,
        limit: int = 50,
        cursor: str | None = None,
        task_class: str | None = None,
        terminal: str | None = None,
    ) -> dict[str, Any]:
        from recertia.graph.engine import GraphOrchestrator

        orch = GraphOrchestrator(runs_root)
        try:
            ids = orch.checkpoints.list_run_ids()
        finally:
            orch.close()
        mem_ids = [rid for (tid, rid) in self.runs if tid == tenant_id]
        all_ids = sorted(set(ids) | set(mem_ids))
        if cursor:
            all_ids = [i for i in all_ids if i > cursor]
        items: list[dict[str, Any]] = []
        for run_id in all_ids:
            if len(items) >= limit:
                break
            rec = self.get(tenant_id, run_id)
            if rec is None:
                continue
            if task_class and rec.task_class != task_class:
                continue
            if terminal and (rec.terminal or "") != terminal:
                continue
            if rec.tenant_id != tenant_id:
                continue
            created = rec.created_at
            items.append(
                {
                    "run_id": rec.run_id,
                    "task_class": rec.task_class,
                    "terminal": rec.terminal,
                    "status": rec.status,
                    "attempt_no": rec.attempt_no,
                    "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
                    "cost_usd": getattr(rec, "cost_usd", None),
                    "arm": rec.arm,
                    "tenant_id": rec.tenant_id,
                }
            )
        next_cursor = items[-1]["run_id"] if items and len(items) == limit else None
        return {"items": items, "next_cursor": next_cursor}
