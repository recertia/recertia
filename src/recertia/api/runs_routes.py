"""``/v1/runs`` create, fetch, and resume. Quota admission goes through ``RunService``."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from recertia.api.auth import Principal
from recertia.api.console_deps import ConsoleContext
from recertia.api.errors import v1_error
from recertia.api.quotas import QuotaExceeded
from recertia.api.runs_models import RunCreate, RunRecord
from recertia.app.runs import AcceptedRun
from recertia.errors import ConfigError, Forbidden, NotFound, RateLimited, RecertiaError
from recertia.graph.engine import RunIdExistsError
from recertia.graph.lease import RunBusy
from recertia.runtime.errors import RunIdExists, WorkspaceBusy
from recertia.solver.sandbox import SandboxError


def _map_run_error(exc: BaseException, *, run_id: str | None = None) -> Any:
    """Translate service errors to the existing HTTP envelope. Returns or raises."""

    from recertia.api import _quota_error

    if isinstance(exc, QuotaExceeded):
        return _quota_error(exc, run_id=run_id)
    if isinstance(exc, RateLimited):
        return v1_error(
            429,
            code="worker_busy",
            message=exc.message,
            run_id=exc.run_id or run_id,
            retryable=True,
        )
    if isinstance(exc, SandboxError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, Forbidden):
        raise HTTPException(status_code=403, detail=exc.message) from exc
    if isinstance(exc, NotFound):
        raise HTTPException(status_code=404, detail=exc.message) from exc
    if isinstance(exc, (RunIdExists, RunIdExistsError)):
        raise HTTPException(status_code=409, detail="run_id already exists") from exc
    if isinstance(exc, WorkspaceBusy):
        raise HTTPException(status_code=409, detail="workspace_busy") from exc
    if isinstance(exc, RunBusy):
        raise HTTPException(status_code=409, detail="run_busy") from exc
    if isinstance(exc, ConfigError):
        raise HTTPException(status_code=400, detail=exc.message) from exc
    if isinstance(exc, RecertiaError):
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc
    raise exc


def register_run_routes(app: FastAPI, ctx: ConsoleContext) -> None:
    router = APIRouter()
    run_service = ctx.run_service
    require_runs = ctx.require_scope("runs", ctx.key_store)

    @router.post("/v1/runs", response_model=None)
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
            return JSONResponse(status_code=result.status_code, content=result.body)
        return result

    @router.get("/v1/runs/{run_id}", response_model=RunRecord)
    def get_run(run_id: str, principal: Principal = Depends(require_runs)) -> RunRecord:
        try:
            run_id = ctx.validate_run_id(run_id)
            loaded = run_service.get(principal.tenant_id, run_id, principal=principal)
        except Exception as exc:
            return _map_run_error(exc, run_id=run_id)
        if loaded is None:
            raise HTTPException(status_code=404, detail="run not found")
        return loaded

    @router.post("/v1/runs/{run_id}/resume", response_model=None)
    def resume_run(run_id: str, principal: Principal = Depends(require_runs)) -> Any:
        try:
            return run_service.resume(principal, run_id)
        except Exception as exc:
            return _map_run_error(exc, run_id=run_id)

    app.include_router(router)
