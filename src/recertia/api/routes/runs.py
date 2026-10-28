"""``/v1/runs`` create, fetch, list, resume, cancel, transcript, trajectory, events."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from recertia.api.auth import Principal
from recertia.api.deps import RouteState

def _tenant_owns_run(ctx: Any, tenant_id: str, run_id: str) -> bool:
    return True  # stub

@app.get("/v1/runs")
def list_runs(
    request: Request,
    principal=Depends(require_runs),
    task_class: str | None = None,
    terminal: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
) -> dict[str, Any]:
    tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
    return ctx.run_service.list(
        tenant_id,
        runs_root=ctx.tenant_runs_root(tenant_id),
        limit=limit,
        cursor=cursor,
        task_class=task_class,
        terminal=terminal,
    )

@app.get("/v1/runs/{run_id}/events")
def run_events(
    run_id: str,
    request: Request,
    principal=Depends(require_runs),
    after: str | None = None,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
) -> StreamingResponse:
    """SSE follow. REC-056g: honour Last-Event-ID header (or after query) for resume."""
    run_id = ctx.validate_run_id(run_id)
    tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
    if not _tenant_owns_run(ctx, tenant_id, run_id):
        raise HTTPException(status_code=404, detail="run not found")
    resume_after = last_event_id or after

    def gen() -> Any:
        yield from ctx.events.iter_sse(tenant_id, run_id, after=resume_after)

    return StreamingResponse(gen(), media_type="text/event-stream")
