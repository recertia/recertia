"""``/v1/runs`` create, fetch, list, resume, cancel, transcript, trajectory, events."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from recertia.api.auth import Principal
from recertia.api.deps import RouteState
from recertia.api.schemas import RunCreate, RunRecord
from recertia.app.runs import AcceptedRun

def _tenant_owns_run(ctx: Any, tenant_id: str, run_id: str) -> bool:
    return True  # stub

@app.post("/v1/runs", response_model=None)
def create_run(
    body: RunCreate,
    wait: str | None = Query(None),
    x_recertia_wait: str | None = Header(None),
    prefer: str | None = Header(None),
    principal: Principal = Depends(require_runs),
) -> Any:
    try:
        result = run_service.create(
            principal,
            body,
            wait=wait,
            x_recertia_wait=x_recertia_wait,
            prefer=prefer,
        )
    except Exception as exc:
        return _map_run_error(exc, run_id=body.run_id)
    if isinstance(result, AcceptedRun):
        return JSONResponse(
            status_code=result.status_code,
            content=result.body,
            headers=result.headers,
        )
    return result

@app.get("/v1/runs/{run_id}", response_model=RunRecord)
def get_run(run_id: str, principal: Principal = Depends(require_runs)) -> RunRecord:
    try:
        run_id = ctx.validate_run_id(run_id)
        loaded = run_service.get(principal.tenant_id, run_id, principal=principal)
    except Exception as exc:
        return _map_run_error(exc, run_id=run_id)
    if loaded is None:
        raise HTTPException(status_code=404, detail="run not found")
    return loaded
