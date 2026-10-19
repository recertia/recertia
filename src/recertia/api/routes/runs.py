"""``/v1/runs`` create, fetch, list, resume, cancel, transcript, trajectory, events."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from recertia.api.auth import Principal
from recertia.api.deps import RouteState
from recertia.api.errors import quota_error, v1_error
from recertia.api.quotas import QuotaExceeded
from recertia.api.schemas import RunCreate, RunRecord
from recertia.app.runs import AcceptedRun
from recertia.errors import ConfigError, Forbidden, NotFound, RateLimited, RecertiaError
from recertia.graph.engine import RunIdExistsError
from recertia.graph.lease import RunBusy
from recertia.runtime.errors import RunIdExists, WorkspaceBusy
from recertia.solver.sandbox import SandboxError
from recertia.solver.transcript import TranscriptStore
from recertia.trajectory.store import TrajectoryStore


def _tenant_owns_run(ctx: Any, tenant_id: str, run_id: str) -> bool:
    if (tenant_id, run_id) in ctx.runs:
        return True
    if ctx.events.exists(tenant_id, run_id):
        return True
    return ctx.load_from_checkpoints(ctx.root, tenant_id, run_id) is not None


def _map_run_error(exc: BaseException, *, run_id: str | None = None) -> Any:
    if isinstance(exc, QuotaExceeded):
        return quota_error(exc, run_id=run_id)
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


def register_runs_routes(app: FastAPI, rs: RouteState) -> None:
    ctx = rs.ctx
    run_service = ctx.run_service
    require_runs = rs.require_runs
    _resolve_tenant = rs.resolve_tenant

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

    @app.post("/v1/runs/{run_id}/resume", response_model=None)
    def resume_run(run_id: str, principal: Principal = Depends(require_runs)) -> Any:
        try:
            result = run_service.resume(principal, run_id)
        except Exception as exc:
            return _map_run_error(exc, run_id=run_id)
        if isinstance(result, AcceptedRun):
            return JSONResponse(
                status_code=result.status_code,
                content=result.body,
                headers=result.headers,
            )
        return result

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

    @app.get("/v1/runs/{run_id}/transcript")
    def run_transcript(
        run_id: str,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        run_id = ctx.validate_run_id(run_id)
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        latest = ctx.checkpoint_reader.latest_row(
            tenant_id, run_id, runs_root=ctx.tenant_runs_root(tenant_id)
        )
        if latest is None:
            raise HTTPException(status_code=404, detail="run not found")
        state = latest[3]
        ref = state.transcript_ref
        store = TranscriptStore(ctx.tenant_runs_root(tenant_id) / "transcripts")
        if not ref:
            return {"run_id": run_id, "events": [], "content_hash": None}
        try:
            payload = store.read(ref)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="transcript not found") from exc
        return {"run_id": run_id, "content_hash": ref, **payload}

    @app.get("/v1/runs/{run_id}/trajectory")
    def run_trajectory(
        run_id: str,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        run_id = ctx.validate_run_id(run_id)
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        store = TrajectoryStore(ctx.tenant_runs_root(tenant_id) / "trajectories")
        traj = store.get_trajectory(run_id)
        if traj is None:
            raise HTTPException(status_code=404, detail="trajectory not found")
        return traj.model_dump(mode="json")

    @app.get("/v1/runs/{run_id}/events")
    def run_events(
        run_id: str,
        request: Request,
        principal=Depends(require_runs),
        after: str | None = None,
        follow: bool = False,
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        run_id = ctx.validate_run_id(run_id)
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        if not _tenant_owns_run(ctx, tenant_id, run_id):
            raise HTTPException(status_code=404, detail="run not found")
        offset = 0
        if last_event_id is not None and last_event_id.strip().isdigit():
            offset = int(last_event_id.strip())
        elif after:
            offset = ctx.events.offset_after_event_id(tenant_id, run_id, after)
        timeout_s = 60.0 if follow or last_event_id is not None else 0.0

        def _terminal() -> bool:
            rec = ctx.runs.get((tenant_id, run_id))
            return bool(rec is not None and getattr(rec, "terminal", None))

        def gen() -> Any:
            yield from ctx.events.follow(
                tenant_id,
                run_id,
                offset=offset,
                timeout_s=timeout_s,
                is_terminal=_terminal,
            )

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/v1/runs/{run_id}/cancel")
    def cancel_run(
        run_id: str,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        run_id = ctx.validate_run_id(run_id)
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        if not _tenant_owns_run(ctx, tenant_id, run_id):
            raise HTTPException(status_code=404, detail="run not found")
        try:
            return run_service.cancel(principal, run_id)
        except Exception as exc:
            return _map_run_error(exc, run_id=run_id)
