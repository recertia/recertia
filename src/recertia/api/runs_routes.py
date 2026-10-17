"""``/v1/runs`` create, fetch, and resume. Quota admission goes through ``RunService``."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from contracts.budget import Budget
from contracts.criteria import TaskCriterion
from recertia.api.auth import Principal
from recertia.api.console_deps import ConsoleContext
from recertia.api.errors import v1_error
from recertia.api.quotas import QuotaExceeded
from recertia.api.runs_models import RunCreate, RunRecord
from recertia.bootstrap import resolve_task_class
from recertia.solver.container import ensure_api_execution_ready
from recertia.solver.sandbox import SandboxError
from recertia.telemetry import get_telemetry

_WAIT_CAP_S = 30.0
_SYNC_WAIT_S = 60.0


def _wait_seconds(
    wait: str | None,
    x_recertia_wait: str | None,
    prefer: str | None,
) -> float | None:
    """Optional enqueue-and-wait budget. Absent means return 202 immediately.

    ``?wait=<seconds>`` or ``X-Recertia-Wait`` is a timeout. ``Prefer: wait``
    uses the cap. The value is never above ``_WAIT_CAP_S``.
    """

    raw: str | None = None
    if wait is not None:
        raw = wait
    elif x_recertia_wait is not None:
        raw = x_recertia_wait
    else:
        for part in (prefer or "").split(","):
            token = part.strip()
            lowered = token.lower()
            if lowered == "wait":
                return _WAIT_CAP_S
            if lowered.startswith("wait="):
                raw = token.split("=", 1)[1].strip()
                break
    if raw is None:
        return None
    text = raw.strip()
    if text == "":
        return _WAIT_CAP_S
    try:
        seconds = float(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="wait must be seconds") from exc
    if seconds < 0:
        raise HTTPException(status_code=400, detail="wait must be seconds")
    return min(seconds, _WAIT_CAP_S)


def register_run_routes(app: FastAPI, ctx: ConsoleContext) -> None:
    from recertia.api import (
        _api_max_criterion_timeout_s,
        _load_persisted_workdir,
        _model_config_for_run,
        _quota_error,
        _tenant_has_checkpoints,
    )

    router = APIRouter()
    runs = ctx.runs
    run_service = ctx.run_service
    root = ctx.root
    require_runs = ctx.require_scope("runs", ctx.key_store)

    def _dispatch_queued(
        *,
        mode: str,
        wait_s: float | None,
        resume_only: bool,
        settle_quota: bool,
        run_id: str,
        run_key: tuple[str, str],
        request: str | None,
        task_class: str,
        criteria: list[TaskCriterion],
        budget: Budget,
        workdir: Any,
        script: list[str] | None,
        arm: Any,
        goal: Any,
        approve_tools: bool,
        model_config: Any,
        workspace_id: str | None,
        has_goal: bool,
        principal: Principal,
    ) -> Any:
        """Enqueue a run and, when ``wait_s`` is set, block until it finishes."""

        from recertia.workers.queue import QueueItem
        from recertia.workers.run_worker import AsyncRunRequest, dump_async_request

        runs[run_key] = RunRecord(
            run_id=run_id,
            request=request,
            task_class=task_class,
            status="queued",
            created_at=datetime.now(timezone.utc),
            tenant_id=principal.tenant_id,
            terminal=None,
            has_goal=has_goal,
            mode=mode,  # type: ignore[arg-type]
            workspace_id=workspace_id,
            workdir=str(workdir),
        )
        queued = AsyncRunRequest(
            run_id=run_id,
            tenant_id=principal.tenant_id,
            goal=goal,
            request=request,
            task_class=task_class,
            criteria=criteria,
            budget=budget,
            workdir=workdir,
            script=script,
            arm=arm,
            approve_tools=approve_tools,
            skills_root=ctx.tenant_skills_root(principal.tenant_id),
            facts_root=ctx.facts_root,
            runs_root=root / "runs" / principal.tenant_id,
            index_path=root / "runs" / principal.tenant_id / "skill_index.db",
            model_config=model_config,
            max_criterion_timeout_s=_api_max_criterion_timeout_s(),
            settle_quota=settle_quota,
            resume_only=resume_only,
            surface="api",
        )
        ctx.events.append(principal.tenant_id, run_id, "run.queued", {"mode": mode})
        try:
            ctx.queue.enqueue(
                QueueItem(
                    tenant_id=principal.tenant_id,
                    run_id=run_id,
                    kind="run",
                    payload=dump_async_request(queued),
                )
            )
        except Exception:
            run_service.release(principal.tenant_id)
            raise
        ctx.embedded.ensure_started()
        if wait_s is None:
            return JSONResponse(
                status_code=202,
                content={
                    "run_id": run_id,
                    "status": "queued",
                    "mode": mode,
                    "tenant_id": principal.tenant_id,
                    "task_class": task_class,
                    "workspace_id": workspace_id,
                    "workdir": str(workdir),
                },
            )
        finished = ctx.queue.wait(principal.tenant_id, run_id, timeout_s=wait_s)
        row = ctx.queue.get(principal.tenant_id, run_id)
        code = row.error_code if row is not None else None
        if code in {"run_busy", "workspace_busy"}:
            runs.pop(run_key, None)
            run_service.release(principal.tenant_id)
            raise HTTPException(status_code=409, detail=code)
        if code == "not_found":
            runs.pop(run_key, None)
            raise HTTPException(status_code=404, detail="run not found")
        if code == "run_failed":
            raise HTTPException(status_code=500, detail="internal server error")
        loaded = run_service.get(principal.tenant_id, run_id)
        if finished is not None and loaded is not None and getattr(loaded, "terminal", None):
            updated = loaded.model_copy(
                update={"mode": mode, "workdir": str(workdir), "workspace_id": workspace_id}
            )
            runs[run_key] = updated
            get_telemetry().emit(
                "run.finished",
                tenant_id=principal.tenant_id,
                run_id=run_id,
                terminal=updated.terminal,
            )
            return updated
        return JSONResponse(
            status_code=202,
            content={
                "run_id": run_id,
                "status": "queued",
                "mode": mode,
                "tenant_id": principal.tenant_id,
                "task_class": task_class,
                "workspace_id": workspace_id,
                "workdir": str(workdir),
            },
        )

    @router.post("/v1/runs", response_model=None)
    def create_run(
        body: RunCreate,
        wait: str | None = Query(None),
        x_recertia_wait: str | None = Header(None),
        prefer: str | None = Header(None),
        principal: Principal = Depends(require_runs),
    ) -> Any:
        try:
            ensure_api_execution_ready()
        except SandboxError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        if body.script and not ctx.principal_may_exec(principal):
            raise HTTPException(
                status_code=403,
                detail="script requires the 'exec' scope (or admin)",
            )

        model_config = _model_config_for_run(body.model)
        run_id = ctx.validate_run_id(body.run_id or f"run-{uuid4().hex[:12]}")
        run_key = (principal.tenant_id, run_id)
        if run_key in runs or _tenant_has_checkpoints(root, principal.tenant_id, run_id):
            raise HTTPException(status_code=409, detail="run_id already exists")

        workdir, wd_meta = ctx.resolve_create_workdir(
            root,
            principal.tenant_id,
            run_id,
            body.workdir,
            workspace_id=body.workspace_id,
            registry=ctx.workspace_registry,
        )
        if wd_meta.get("kind") != "registered":
            workdir.mkdir(parents=True, exist_ok=True)
        ctx.persist_workdir(root, principal.tenant_id, run_id, workdir, meta=wd_meta)

        request = body.request
        if body.goal is not None and body.goal.context and not request:
            request = body.goal.context

        task_class = resolve_task_class(
            explicit=body.task_class,
            goal_task_class=body.goal.task_class if body.goal else None,
        )
        criteria = ctx.clamp_criteria(
            [TaskCriterion.model_validate(item) for item in (body.criteria or [])]
        )
        budget = Budget.model_validate(body.budget) if body.budget else Budget()
        approve_tools = ctx.principal_may_exec(principal)

        sync = body.mode != "async"
        wait_s = _SYNC_WAIT_S if sync else _wait_seconds(wait, x_recertia_wait, prefer)
        try:
            run_service.admit(principal.tenant_id)
        except QuotaExceeded as exc:
            return _quota_error(exc, run_id=run_id)
        if sync and not ctx.run_slots.acquire(blocking=False):
            run_service.release(principal.tenant_id)
            return v1_error(
                429,
                code="worker_busy",
                message="too many in-flight runs",
                run_id=run_id,
                retryable=True,
            )
        try:
            return _dispatch_queued(
                mode="sync" if sync else "async",
                wait_s=wait_s,
                resume_only=False,
                settle_quota=True,
                run_id=run_id,
                run_key=run_key,
                request=request,
                task_class=task_class,
                criteria=criteria,
                budget=budget,
                workdir=workdir,
                script=body.script,
                arm=body.arm,
                goal=body.goal,
                approve_tools=approve_tools,
                model_config=model_config,
                workspace_id=body.workspace_id,
                has_goal=body.goal is not None,
                principal=principal,
            )
        finally:
            if sync:
                ctx.run_slots.release()

    @router.get("/v1/runs/{run_id}", response_model=RunRecord)
    def get_run(run_id: str, principal: Principal = Depends(require_runs)) -> RunRecord:
        run_id = ctx.validate_run_id(run_id)
        loaded = run_service.get(principal.tenant_id, run_id)
        if loaded is None:
            raise HTTPException(status_code=404, detail="run not found")
        return loaded

    @router.post("/v1/runs/{run_id}/resume", response_model=None)
    def resume_run(run_id: str, principal: Principal = Depends(require_runs)) -> Any:
        try:
            ensure_api_execution_ready()
        except SandboxError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        run_id = ctx.validate_run_id(run_id)
        run_key = (principal.tenant_id, run_id)
        workdir = _load_persisted_workdir(
            root, principal.tenant_id, run_id, registry=ctx.workspace_registry
        )
        if workdir is None:
            workdir = ctx.canonical_run_workdir(root, principal.tenant_id, run_id)
        if not workdir.exists():
            raise HTTPException(status_code=404, detail="run workdir not found")
        existing = runs.get(run_key)
        try:
            run_service.admit(principal.tenant_id)
        except QuotaExceeded as exc:
            return _quota_error(exc, run_id=run_id)
        if not ctx.run_slots.acquire(blocking=False):
            run_service.release(principal.tenant_id)
            return v1_error(
                429,
                code="worker_busy",
                message="too many in-flight runs",
                run_id=run_id,
                retryable=True,
            )
        try:
            return _dispatch_queued(
                mode="sync",
                wait_s=_SYNC_WAIT_S,
                resume_only=True,
                settle_quota=False,
                run_id=run_id,
                run_key=run_key,
                request=existing.request if existing else run_id,
                task_class=existing.task_class if existing else "repo-chore",
                criteria=[],
                budget=Budget(),
                workdir=workdir,
                script=None,
                arm="treatment",
                goal=None,
                approve_tools=ctx.principal_may_exec(principal),
                model_config=None,
                workspace_id=None,
                has_goal=bool(existing.has_goal) if existing else False,
                principal=principal,
            )
        finally:
            ctx.run_slots.release()

    app.include_router(router)
