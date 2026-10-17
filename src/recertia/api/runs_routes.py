"""``/v1/runs`` create, fetch, and resume. Quota admission goes through ``RunService``."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from contracts.budget import Budget
from contracts.criteria import TaskCriterion
from contracts.run import Task
from recertia.api.auth import Principal
from recertia.api.console_deps import ConsoleContext
from recertia.api.errors import v1_error
from recertia.api.quotas import QuotaExceeded
from recertia.api.runs_models import RunCreate, RunRecord
from recertia.app.authz import Resource
from recertia.bootstrap import resolve_task_class
from recertia.errors import Forbidden
from recertia.graph.engine import RunIdExistsError
from recertia.graph.lease import RunBusy
from recertia.persistence.layout import RuntimeLayout
from recertia.runtime.errors import RunIdExists, WorkspaceBusy
from recertia.runtime.registry import RunRegistry
from recertia.runtime.workspace_lock import WorkspaceLock, WorkspaceLockHandle
from recertia.solver.container import ensure_api_execution_ready
from recertia.solver.sandbox import SandboxError
from recertia.telemetry import get_telemetry

_log = logging.getLogger("recertia.api")
_WAIT_CAP_S = 30.0


def _create_request_hash(body: RunCreate, run_id: str) -> str:
    payload = {
        "run_id": run_id,
        "request": body.request,
        "goal": body.goal.model_dump(mode="json") if body.goal is not None else None,
        "script": body.script,
        "task_class": body.task_class,
        "workspace_id": body.workspace_id,
        "workdir": body.workdir,
        "arm": body.arm,
        "mode": body.mode,
    }
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _reserve_run_id(
    root: Path,
    *,
    tenant_id: str,
    run_id: str,
    body: RunCreate,
    workdir: Path,
) -> None:
    """Durable uniqueness via ``runtime.db``. Survives process restart."""

    registry = RunRegistry(RuntimeLayout(root).tenant(tenant_id).runtime_db)
    try:
        registry.reserve(
            tenant_id,
            run_id,
            request_hash=_create_request_hash(body, run_id),
            summary_json={
                "run_id": run_id,
                "status": "accepted",
                "tenant_id": tenant_id,
            },
            workdir=workdir,
        )
    except RunIdExists as exc:
        raise HTTPException(status_code=409, detail="run_id already exists") from exc
    finally:
        registry.close()


def _acquire_workspace(
    root: Path,
    workdir: Path,
    *,
    owner_id: str,
) -> WorkspaceLockHandle:
    try:
        return WorkspaceLock(RuntimeLayout(root).locks_dir).acquire(
            workdir, owner_id=owner_id
        )
    except WorkspaceBusy as exc:
        raise HTTPException(status_code=409, detail="workspace_busy") from exc


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


def _build_orchestrator():
    """Resolve at call time so tests can patch ``recertia.api.build_default_orchestrator``."""

    import recertia.api as api_mod

    return api_mod.build_default_orchestrator


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

        try:
            ctx.pdp.require(
                principal, "runs.create", Resource(tenant_id=principal.tenant_id)
            )
            if body.script:
                ctx.pdp.require(
                    principal, "runs.script", Resource(tenant_id=principal.tenant_id)
                )
        except Forbidden as exc:
            raise HTTPException(status_code=403, detail=exc.message) from exc

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
        # Restart-safe uniqueness (F9). In-memory / checkpoint checks above remain.
        _reserve_run_id(
            root,
            tenant_id=principal.tenant_id,
            run_id=run_id,
            body=body,
            workdir=workdir,
        )
        owner_id = f"{principal.tenant_id}/{run_id}"
        ws_handle: WorkspaceLockHandle | None = _acquire_workspace(
            root, workdir, owner_id=owner_id
        )
        try:
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

            if body.mode == "async":
                wait_s = _wait_seconds(wait, x_recertia_wait, prefer)
                try:
                    run_service.admit(principal.tenant_id)
                except QuotaExceeded as exc:
                    return _quota_error(exc, run_id=run_id)
                runs[run_key] = RunRecord(
                    run_id=run_id,
                    request=request,
                    task_class=task_class,
                    status="queued",
                    created_at=datetime.now(timezone.utc),
                    tenant_id=principal.tenant_id,
                    terminal=None,
                    has_goal=body.goal is not None,
                    mode="async",
                    workspace_id=body.workspace_id,
                    workdir=str(workdir),
                )
                from recertia.workers.queue import QueueItem
                from recertia.workers.run_worker import AsyncRunRequest, dump_async_request

                queued = AsyncRunRequest(
                    run_id=run_id,
                    tenant_id=principal.tenant_id,
                    goal=body.goal,
                    request=request,
                    task_class=body.task_class,
                    criteria=criteria,
                    budget=budget,
                    workdir=workdir,
                    script=body.script,
                    arm=body.arm,
                    approve_tools=approve_tools,
                    skills_root=ctx.tenant_skills_root(principal.tenant_id),
                    facts_root=ctx.facts_root,
                    runs_root=root / "runs" / principal.tenant_id,
                    index_path=root / "runs" / principal.tenant_id / "skill_index.db",
                    model_config=model_config,
                    max_criterion_timeout_s=_api_max_criterion_timeout_s(),
                )
                ctx.events.append(principal.tenant_id, run_id, "run.queued", {"mode": "async"})
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
                # Release before wait/202 so an embedded worker can later take the
                # same workdir lock. TODO(REL-3): worker re-acquires for the lease.
                held = ws_handle
                ws_handle = None
                if held is not None:
                    held.release()
                if wait_s is not None:
                    finished = ctx.queue.wait(principal.tenant_id, run_id, timeout_s=wait_s)
                    if finished is not None:
                        loaded = run_service.get(principal.tenant_id, run_id)
                        if loaded is not None and getattr(loaded, "terminal", None):
                            return loaded
                return JSONResponse(
                    status_code=202,
                    content={
                        "run_id": run_id,
                        "status": "queued",
                        "mode": "async",
                        "tenant_id": principal.tenant_id,
                        "task_class": task_class,
                        "workspace_id": body.workspace_id,
                        "workdir": str(workdir),
                    },
                )

            task = Task(
                task_id=run_id,
                goal=body.goal,
                request=request,
                task_class=task_class,
                submitted_at=datetime.now(timezone.utc),
                submitted_by=principal.key_id,
            )
            get_telemetry().emit(
                "run.started",
                tenant_id=principal.tenant_id,
                run_id=run_id,
                task_class=task_class,
            )
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

            bundle = _build_orchestrator()(
                root / "runs" / principal.tenant_id,
                skills_root=ctx.tenant_skills_root(principal.tenant_id),
                facts_root=ctx.facts_root,
                index_path=root / "runs" / principal.tenant_id / "skill_index.db",
                approve_default_tools=approve_tools,
                principal=principal,
                surface="api",
                model_config=model_config,
            )
            bundle.orchestrator.max_criterion_timeout_s = _api_max_criterion_timeout_s()
            cost_usd = 0.0
            succeeded = False
            try:
                state = bundle.orchestrator.start(
                    run_id,
                    task,
                    criteria,
                    budget=budget,
                    workdir=workdir,
                    script=body.script,
                    arm=body.arm,
                    manifest=bundle.run_manifest(),
                )
                cost_usd = float(state.spent.cost_usd or 0.0)
                succeeded = True
            except RunIdExistsError as exc:
                raise HTTPException(status_code=409, detail="run_id already exists") from exc
            except RunBusy as exc:
                raise HTTPException(status_code=409, detail="run_busy") from exc
            except Exception as exc:
                get_telemetry().emit(
                    "run.finished",
                    tenant_id=principal.tenant_id,
                    run_id=run_id,
                    terminal="error",
                )
                _log.exception(
                    "create_run failed tenant_id=%s run_id=%s",
                    principal.tenant_id,
                    run_id,
                )
                raise HTTPException(status_code=500, detail="internal server error") from exc
            finally:
                bundle.close()
                ctx.run_slots.release()
                if succeeded:
                    run_service.complete(principal.tenant_id, cost_usd=cost_usd)
                else:
                    run_service.release(principal.tenant_id)

            rec = ctx.record_from_state(
                run_id=run_id,
                request=request,
                task_class=task_class,
                tenant_id=principal.tenant_id,
                state=state,
                created_at=datetime.now(timezone.utc),
                has_goal=body.goal is not None,
            )
            rec = rec.model_copy(
                update={
                    "cost_usd": cost_usd,
                    "mode": "sync",
                    "workspace_id": body.workspace_id,
                    "workdir": str(workdir),
                }
            )
            runs[run_key] = rec
            get_telemetry().emit(
                "run.finished",
                tenant_id=principal.tenant_id,
                run_id=run_id,
                terminal=state.terminal,
            )
            return rec
        finally:
            if ws_handle is not None:
                ws_handle.release()

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
        owner_id = f"{principal.tenant_id}/{run_id}"
        ws_handle = _acquire_workspace(root, workdir, owner_id=owner_id)
        try:
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
            bundle = None
            try:
                bundle = _build_orchestrator()(
                    root / "runs" / principal.tenant_id,
                    skills_root=ctx.tenant_skills_root(principal.tenant_id),
                    facts_root=ctx.facts_root,
                    index_path=root / "runs" / principal.tenant_id / "skill_index.db",
                    approve_default_tools=ctx.principal_may_exec(principal),
                    principal=principal,
                    surface="api",
                )
                bundle.orchestrator.max_criterion_timeout_s = _api_max_criterion_timeout_s()
                state = bundle.orchestrator.resume(run_id, workdir=workdir)
            except RunBusy as exc:
                raise HTTPException(status_code=409, detail="run_busy") from exc
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except Exception as exc:
                _log.exception(
                    "resume_run failed tenant_id=%s run_id=%s",
                    principal.tenant_id,
                    run_id,
                )
                raise HTTPException(status_code=500, detail="internal server error") from exc
            finally:
                if bundle is not None:
                    bundle.close()
                ctx.run_slots.release()
                run_service.release(principal.tenant_id)

            existing = runs.get(run_key)
            rec = ctx.record_from_state(
                run_id=run_id,
                request=existing.request if existing else state.task.request,
                task_class=(
                    existing.task_class if existing else (state.task.task_class or "repo-chore")
                ),
                tenant_id=principal.tenant_id,
                state=state,
                created_at=existing.created_at if existing else datetime.now(timezone.utc),
                has_goal=existing.has_goal if existing else (state.task.goal is not None),
            )
            runs[run_key] = rec
            return rec
        finally:
            ws_handle.release()

    app.include_router(router)
