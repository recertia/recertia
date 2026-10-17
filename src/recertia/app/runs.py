"""Run admission, lookup, and listing. Routes do not call the quota store."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Protocol


class RunQuota(Protocol):
    def admit(self, tenant_id: str, *, quota: Any = None) -> None: ...

    def release_inflight(self, tenant_id: str) -> None: ...

    def complete(self, tenant_id: str, *, cost_usd: float = 0.0) -> None: ...


def _summary_from_record(rec: Any) -> dict[str, Any]:
    created = rec.created_at
    return {
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
        from recertia.graph.store import CheckpointStore

        store = CheckpointStore(Path(runs_root) / "checkpoints.db")
        try:
            disk_rows = store.list_summaries(
                limit=limit + 1,
                cursor=cursor,
                task_class=task_class,
                terminal=terminal,
            )
        finally:
            store.close()
        disk_has_extra = len(disk_rows) > limit
        page = disk_rows[:limit]
        next_disk_id = str(disk_rows[limit]["run_id"]) if disk_has_extra else None
        chosen: dict[str, dict[str, Any]] = {
            str(row["run_id"]): {**row, "tenant_id": tenant_id} for row in page
        }
        for (tid, run_id), rec in self.runs.items():
            if tid != tenant_id or rec is None:
                continue
            if getattr(rec, "tenant_id", tenant_id) != tenant_id:
                continue
            if cursor and not run_id > cursor:
                continue
            if next_disk_id is not None and run_id >= next_disk_id:
                continue
            if task_class and getattr(rec, "task_class", None) != task_class:
                continue
            if terminal and (getattr(rec, "terminal", None) or "") != terminal:
                continue
            chosen[run_id] = _summary_from_record(rec)
        ordered = sorted(chosen.values(), key=lambda row: str(row["run_id"]))
        has_more = disk_has_extra or len(ordered) > limit
        items = ordered[:limit]
        next_cursor = items[-1]["run_id"] if items and has_more else None
        return {"items": items, "next_cursor": next_cursor}
