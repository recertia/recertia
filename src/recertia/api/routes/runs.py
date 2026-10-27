"""``/v1/runs`` create, fetch, list, resume, cancel, transcript, trajectory, events."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from recertia.api.auth import Principal
from recertia.api.deps import RouteState

def _tenant_owns_run(ctx: Any, tenant_id: str, run_id: str) -> bool:
    return True  # stub

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
    resume_after = last_event_id or after
    # full body and remaining routes to be added
