"""FastAPI surface: health, runs (GraphOrchestrator), metrics dashboard, blob digests.

``POST /v1/runs`` executes offline via ``GraphOrchestrator.start`` (same path as
``recertia run``), not an enqueue-only stub. Optional CLI-parity fields: ``goal``,
``task_class`` (falls back to ``goal.task_class``, then ``repo-chore``), ``criteria``,
``script``, ``budget``, ``workdir``, ``arm``.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from contracts.budget import Budget
from contracts.common import Arm
from contracts.criteria import TaskCriterion
from contracts.goal import Goal
from contracts.run import Task
from recertia.api.auth import ApiKeyStore, Principal, require_scope, validate_tenant_id
from recertia.api.console_routes import ConsoleContext, register_console_routes
from recertia.api.errors import V1HTTPError, envelope_http_exception, v1_error
from recertia.api.quotas import QuotaExceeded, QuotaStore
from recertia.api.remaining_routes import register_remaining_routes
from recertia.bootstrap import build_default_orchestrator, resolve_task_class
from recertia.config import ModelConfig
from recertia.graph.engine import GraphOrchestrator
from recertia.ids import InvalidIdError, validate_run_id
from recertia.paths import HostRootError, looks_absolute, resolve_under_host_root
from recertia.solver.container import configured_backend, ensure_api_execution_ready
from recertia.solver.sandbox import SandboxError
from recertia.store.blobs import FilesystemBlobStore, normalize_blob_digest
from recertia.telemetry import get_telemetry, render_dashboard
from recertia.workspaces.registry import WorkspaceRegistry

DEFAULT_ROOT = Path(".recertia")
_MAX_BLOB_BYTES = int(os.environ.get("RECERTIA_MAX_BLOB_BYTES", str(16 * 1024 * 1024)))
_API_MAX_CRITERION_TIMEOUT_S = int(os.environ.get("RECERTIA_API_MAX_CRITERION_TIMEOUT_S", "300"))
_MAX_IN_FLIGHT_RUNS = int(os.environ.get("RECERTIA_API_MAX_IN_FLIGHT_RUNS", "4"))


def _validate_run_id(run_id: str) -> str:
    try:
        return validate_run_id(run_id)
    except InvalidIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _clamp_criteria_for_api(criteria: list[TaskCriterion]) -> list[TaskCriterion]:
    clamped: list[TaskCriterion] = []
    for criterion in criteria:
        if criterion.timeout_s > _API_MAX_CRITERION_TIMEOUT_S:
            clamped.append(
                criterion.model_copy(update={"timeout_s": _API_MAX_CRITERION_TIMEOUT_S})
            )
        else:
            clamped.append(criterion)
    return clamped


def _principal_may_exec(principal: Principal) -> bool:
    return "exec" in principal.scopes or "admin" in principal.scopes


def _tenant_blob_root(root: Path, tenant_id: str) -> Path:
    """Resolve a tenant blob directory and refuse path escape."""

    validate_tenant_id(tenant_id)
    base = (root / "blobs").resolve()
    candidate = (base / tenant_id).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid tenant blob root") from exc
    return candidate


def _workspaces_root(root: Path) -> Path:
    return (root / "workspaces").resolve()


def _canonical_run_workdir(root: Path, tenant_id: str, run_id: str) -> Path:
    """Always resolve under ``root/workspaces/<tenant_id>/<run_id>``; reject escapes."""

    validate_tenant_id(tenant_id)
    run_id = _validate_run_id(run_id)
    base = _workspaces_root(root)
    candidate = (base / tenant_id / run_id).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="workdir escapes workspaces root") from exc
    return candidate


def _resolve_sandbox_workdir(
    root: Path, tenant_id: str, run_id: str, workdir: str | None
) -> Path:
    """Map optional caller ``workdir`` under the canonical run workspace only."""

    base = _canonical_run_workdir(root, tenant_id, run_id)
    if workdir is None or workdir == "":
        return base
    if looks_absolute(workdir):
        raise HTTPException(
            status_code=400,
            detail="workdir must be relative to the run workspace (absolute paths rejected)",
        )
    ref = Path(workdir)
    candidate = (base / ref).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="workdir escapes run workspace") from exc
    return candidate


def _resolve_create_workdir(
    root: Path,
    tenant_id: str,
    run_id: str,
    workdir: str | None,
    *,
    workspace_id: str | None = None,
    registry: WorkspaceRegistry | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Resolve create-run workdir; return ``(path, workdir.json payload)``."""

    if workspace_id:
        if registry is None:
            raise HTTPException(status_code=500, detail="workspace registry unavailable")
        ws = registry.get(workspace_id, tenant_id=tenant_id, enabled_only=False)
        if ws is None:
            raise HTTPException(status_code=404, detail="workspace not found")
        if not ws.enabled:
            raise HTTPException(status_code=403, detail="workspace disabled")
        try:
            effective = resolve_under_host_root(ws.host_root, workdir)
        except HostRootError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not effective.is_dir():
            raise HTTPException(status_code=400, detail="registered workdir missing")
        meta = {
            "kind": "registered",
            "workspace_id": ws.workspace_id,
            "subpath": workdir or "",
            "workdir": str(effective.resolve()),
            "host_root": ws.host_root,
        }
        return effective, meta

    if workdir is not None and looks_absolute(workdir):
        raise HTTPException(
            status_code=400,
            detail="workdir must be relative to the run workspace (absolute paths rejected)",
        )
    effective = _resolve_sandbox_workdir(root, tenant_id, run_id, workdir)
    meta = {"kind": "sandbox", "workdir": str(effective.resolve())}
    return effective, meta


def _workdir_meta_path(root: Path, tenant_id: str, run_id: str) -> Path:
    return root / "runs" / tenant_id / run_id / "workdir.json"


def _persist_workdir(
    root: Path,
    tenant_id: str,
    run_id: str,
    workdir: Path,
    *,
    meta: dict[str, Any] | None = None,
) -> None:
    payload = dict(meta) if meta is not None else {"kind": "sandbox", "workdir": str(workdir.resolve())}
    payload.setdefault("workdir", str(workdir.resolve()))
    dest = _workdir_meta_path(root, tenant_id, run_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _load_persisted_workdir(
    root: Path,
    tenant_id: str,
    run_id: str,
    *,
    registry: WorkspaceRegistry | None = None,
) -> Path | None:
    meta_path = _workdir_meta_path(root, tenant_id, run_id)
    if not meta_path.exists():
        return None
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        kind = str(payload.get("kind") or "sandbox")
    except (json.JSONDecodeError, OSError, TypeError):
        return None

    if kind == "registered":
        if registry is None:
            raise HTTPException(status_code=500, detail="workspace registry unavailable")
        workspace_id = str(payload.get("workspace_id") or "")
        stored_host = str(payload.get("host_root") or "")
        subpath = payload.get("subpath")
        ws = registry.get(workspace_id, tenant_id=tenant_id, enabled_only=False)
        if ws is None:
            raise HTTPException(status_code=409, detail="workspace not found")
        if not ws.enabled:
            raise HTTPException(status_code=409, detail="workspace disabled")
        if stored_host and ws.host_root != stored_host:
            raise HTTPException(status_code=409, detail="workspace host_root changed")
        try:
            effective = resolve_under_host_root(ws.host_root, str(subpath) if subpath is not None else "")
        except HostRootError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not effective.is_dir():
            raise HTTPException(status_code=409, detail="registered workdir missing")
        return effective

    try:
        stored = Path(str(payload["workdir"])).resolve()
    except (KeyError, OSError, TypeError):
        return None
    base = _canonical_run_workdir(root, tenant_id, run_id)
    try:
        stored.relative_to(base)
    except ValueError:
        return None
    return stored


def _model_config_for_run(model: str | None) -> ModelConfig | None:
    """Apply a console-selected slug after the server allowlist (OG-11). Omitted → env."""

    if model is None or not str(model).strip():
        return None
    from recertia.solver.model_allowlist import (
        ModelAllowlistError,
        config_for_allowed_model,
        ensure_model_allowed,
    )

    try:
        ensure_model_allowed(model)
        return config_for_allowed_model(model)
    except ModelAllowlistError as exc:
        raise V1HTTPError(400, code=exc.code, message=str(exc)) from exc
    except ValueError as exc:
        raise V1HTTPError(400, code="invalid_model", message=str(exc)) from exc


class RunCreate(BaseModel):
    goal: Goal | None = None
    request: str | None = None
    task_class: str | None = None
    criteria: list[dict[str, Any]] | None = None
    script: list[str] | None = None
    budget: dict[str, Any] | None = None
    workdir: str | None = None
    workspace_id: str | None = None
    run_id: str | None = None
    arm: Arm = "treatment"
    mode: str = "sync"  # sync | async (console C2)
    model: str | None = None  # console-selected provider:slug; omitted → process env

    @model_validator(mode="after")
    def _require_goal_or_request(self) -> "RunCreate":
        if self.goal is None and (self.request is None or not self.request.strip()):
            raise ValueError("Provide goal or non-empty request")
        if self.mode not in {"sync", "async"}:
            raise ValueError("mode must be sync or async")
        return self


class RunRecord(BaseModel):
    run_id: str
    request: str | None = None
    task_class: str
    status: str
    created_at: datetime
    tenant_id: str
    terminal: str | None = None
    failure_class: str | None = None
    attempt_no: int | None = None
    arm: str | None = None
    route_log: list[dict[str, Any]] | None = None
    has_goal: bool = False
    cost_usd: float | None = None
    mode: str | None = None
    criteria_hash: str | None = None
    workspace_id: str | None = None
    workdir: str | None = None


def create_app(
    *,
    root: Path | None = None,
    skills_root: Path | None = None,
    facts_root: Path | None = None,
) -> FastAPI:
    root = root or DEFAULT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    skills_root = Path(skills_root) if skills_root is not None else Path("skills")
    facts_root = Path(facts_root) if facts_root is not None else Path("facts")
    key_store = ApiKeyStore(root / "api_keys.sqlite")
    quota_store = QuotaStore(root / "quotas.sqlite")
    workspace_registry = WorkspaceRegistry(root / "workspaces_registry.sqlite")
    blobs_by_tenant: dict[str, FilesystemBlobStore] = {}
    # Keyed by (tenant_id, run_id) so tenants cannot collide on run_id.
    runs: dict[tuple[str, str], RunRecord] = {}
    run_slots = threading.Semaphore(_MAX_IN_FLIGHT_RUNS)
    app = FastAPI(title="Recertia", version="0.1.0")

    console_ctx = ConsoleContext(
        root=root,
        skills_root=skills_root,
        facts_root=facts_root,
        key_store=key_store,
        quota_store=quota_store,
        runs=runs,
        run_slots=run_slots,
        record_from_state=_record_from_state,
        load_from_checkpoints=_load_from_checkpoints,
        resolve_create_workdir=_resolve_create_workdir,
        persist_workdir=_persist_workdir,
        canonical_run_workdir=_canonical_run_workdir,
        clamp_criteria=_clamp_criteria_for_api,
        principal_may_exec=_principal_may_exec,
        require_scope=require_scope,
        validate_run_id=_validate_run_id,
        workspace_registry=workspace_registry,
    )
    register_console_routes(app, console_ctx)
    register_remaining_routes(app, console_ctx)

    @app.exception_handler(V1HTTPError)
    async def _v1_http_error(_request: Any, exc: V1HTTPError) -> Any:
        return exc.response()

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Any, exc: StarletteHTTPException) -> Any:
        return envelope_http_exception(request, exc)

    console_dir = Path(__file__).resolve().parents[3] / "console" / "static"
    if console_dir.is_dir():
        app.mount("/console/assets", StaticFiles(directory=console_dir), name="console-assets")

        @app.get("/console")
        @app.get("/console/")
        def console_index() -> FileResponse:
            return FileResponse(console_dir / "index.html")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/runs", response_model=None)
    def create_run(
        body: RunCreate, principal: Principal = Depends(require_scope("runs", key_store))
    ) -> Any:
        try:
            ensure_api_execution_ready()
        except SandboxError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        if body.script and not _principal_may_exec(principal):
            raise HTTPException(
                status_code=403,
                detail="script requires the 'exec' scope (or admin)",
            )

        model_config = _model_config_for_run(body.model)

        run_id = _validate_run_id(body.run_id or f"run-{uuid4().hex[:12]}")
        run_key = (principal.tenant_id, run_id)
        if run_key in runs:
            raise HTTPException(status_code=409, detail="run_id already exists")

        workdir, wd_meta = _resolve_create_workdir(
            root,
            principal.tenant_id,
            run_id,
            body.workdir,
            workspace_id=body.workspace_id,
            registry=workspace_registry,
        )
        if wd_meta.get("kind") != "registered":
            workdir.mkdir(parents=True, exist_ok=True)
        _persist_workdir(root, principal.tenant_id, run_id, workdir, meta=wd_meta)

        request = body.request
        if body.goal is not None and body.goal.context and not request:
            request = body.goal.context

        task_class = resolve_task_class(
            explicit=body.task_class,
            goal_task_class=body.goal.task_class if body.goal else None,
        )
        criteria = _clamp_criteria_for_api(
            [TaskCriterion.model_validate(c) for c in (body.criteria or [])]
        )
        budget = Budget.model_validate(body.budget) if body.budget else Budget()
        approve_tools = _principal_may_exec(principal) or configured_backend() == "container"

        # ----- async path (console C2) -----
        if body.mode == "async":
            try:
                quota_store.admit(principal.tenant_id)
            except QuotaExceeded as exc:
                return _quota_error(exc, run_id=run_id)
            placeholder = RunRecord(
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
            from recertia.workers.run_worker import AsyncRunRequest

            runs[run_key] = placeholder
            console_ctx.worker.submit(
                AsyncRunRequest(
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
                    skills_root=skills_root,
                    facts_root=facts_root,
                    runs_root=root / "runs" / principal.tenant_id,
                    index_path=root / "runs" / principal.tenant_id / "skill_index.db",
                    model_config=model_config,
                )
            )
            from fastapi.responses import JSONResponse

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
            quota_store.admit(principal.tenant_id)
        except QuotaExceeded as exc:
            return _quota_error(exc, run_id=run_id)

        if not run_slots.acquire(blocking=False):
            quota_store.release_inflight(principal.tenant_id)
            return v1_error(
                429,
                code="worker_busy",
                message="too many in-flight runs",
                run_id=run_id,
                retryable=True,
            )

        # Container backend isolates tools; local API requires exec (or admin) for grants.
        bundle = build_default_orchestrator(
            root / "runs" / principal.tenant_id,
            skills_root=skills_root,
            facts_root=facts_root,
            index_path=root / "runs" / principal.tenant_id / "skill_index.db",
            approve_default_tools=approve_tools,
            model_config=model_config,
        )
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
        except Exception:
            get_telemetry().emit(
                "run.finished",
                tenant_id=principal.tenant_id,
                run_id=run_id,
                terminal="error",
            )
            raise HTTPException(status_code=500, detail="internal server error") from None
        finally:
            bundle.close()
            run_slots.release()
            if succeeded:
                quota_store.complete(principal.tenant_id, cost_usd=cost_usd)
            else:
                quota_store.release_inflight(principal.tenant_id)

        rec = _record_from_state(
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

    @app.get("/v1/runs/{run_id}", response_model=RunRecord)
    def get_run(run_id: str, principal: Principal = Depends(require_scope("runs", key_store))) -> RunRecord:
        run_id = _validate_run_id(run_id)
        run_key = (principal.tenant_id, run_id)
        if run_key in runs:
            return runs[run_key]
        loaded = _load_from_checkpoints(root, principal.tenant_id, run_id)
        if loaded is None:
            raise HTTPException(status_code=404, detail="run not found")
        runs[run_key] = loaded
        return loaded

    @app.post("/v1/runs/{run_id}/resume", response_model=None)
    def resume_run(
        run_id: str, principal: Principal = Depends(require_scope("runs", key_store))
    ) -> Any:
        try:
            ensure_api_execution_ready()
        except SandboxError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        run_id = _validate_run_id(run_id)
        run_key = (principal.tenant_id, run_id)
        # Resume MUST reuse the persisted create workdir — never invent a new path.
        workdir = _load_persisted_workdir(
            root, principal.tenant_id, run_id, registry=workspace_registry
        )
        if workdir is None:
            workdir = _canonical_run_workdir(root, principal.tenant_id, run_id)
        if not workdir.exists():
            raise HTTPException(status_code=404, detail="run workdir not found")
        if not run_slots.acquire(blocking=False):
            return v1_error(
                429,
                code="worker_busy",
                message="too many in-flight runs",
                run_id=run_id,
                retryable=True,
            )
        bundle = build_default_orchestrator(
            root / "runs" / principal.tenant_id,
            skills_root=skills_root,
            facts_root=facts_root,
            index_path=root / "runs" / principal.tenant_id / "skill_index.db",
            approve_default_tools=(
                _principal_may_exec(principal) or configured_backend() == "container"
            ),
        )
        try:
            state = bundle.orchestrator.resume(run_id, workdir=workdir)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception:
            raise HTTPException(status_code=500, detail="internal server error") from None
        finally:
            bundle.close()
            run_slots.release()

        existing = runs.get(run_key)
        rec = _record_from_state(
            run_id=run_id,
            request=existing.request if existing else state.task.request,
            task_class=(existing.task_class if existing else (state.task.task_class or "repo-chore")),
            tenant_id=principal.tenant_id,
            state=state,
            created_at=existing.created_at if existing else datetime.now(timezone.utc),
            has_goal=existing.has_goal if existing else (state.task.goal is not None),
        )
        runs[run_key] = rec
        return rec

    @app.post("/v1/blobs")
    def put_blob(
        payload: dict[str, Any],
        principal: Principal = Depends(require_scope("blobs", key_store)),
    ) -> dict[str, str]:
        blobs = blobs_by_tenant.setdefault(
            principal.tenant_id, FilesystemBlobStore(_tenant_blob_root(root, principal.tenant_id))
        )
        data = str(payload.get("data", "")).encode()
        if len(data) > _MAX_BLOB_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"blob exceeds max size of {_MAX_BLOB_BYTES} bytes",
            )
        digest = blobs.put(data, content_type=str(payload.get("content_type", "text/plain")))
        return {"digest": digest}

    @app.get("/v1/blobs/{digest}")
    def get_blob(
        digest: str, principal: Principal = Depends(require_scope("blobs", key_store))
    ) -> dict[str, Any]:
        try:
            key = normalize_blob_digest(digest)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="blob not found") from exc
        blobs = blobs_by_tenant.setdefault(
            principal.tenant_id, FilesystemBlobStore(_tenant_blob_root(root, principal.tenant_id))
        )
        try:
            data = blobs.get(key)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="blob not found") from exc
        return {"digest": key, "size": len(data), "text": data.decode(errors="replace")[:8000]}

    @app.get("/v1/metrics/dashboard")
    def dashboard(
        principal: Principal = Depends(require_scope("metrics", key_store)),
    ) -> dict[str, Any]:
        payload = render_dashboard(get_telemetry(), tenant_id=principal.tenant_id)
        payload["panels"].append(
            {
                "id": "tenant_quota",
                "type": "stat",
                "values": quota_store.snapshot(principal.tenant_id),
            }
        )
        return payload

    app.state.root = root
    app.state.skills_root = skills_root
    app.state.facts_root = facts_root
    app.state.api_keys = key_store
    app.state.quota_store = quota_store
    app.state.workspace_registry = workspace_registry
    app.state.blobs_by_tenant = blobs_by_tenant
    app.state.runs = runs
    app.state.console_ctx = console_ctx
    return app


def _quota_error(exc: QuotaExceeded, *, run_id: str | None = None) -> Any:
    message = str(exc)
    if "max_in_flight" in message:
        return v1_error(
            429, code="worker_busy", message=message, run_id=run_id, retryable=True
        )
    return v1_error(
        429, code="budget_exhausted", message=message, run_id=run_id, retryable=False
    )


def _record_from_state(
    *,
    run_id: str,
    request: str | None,
    task_class: str,
    tenant_id: str,
    state: Any,
    created_at: datetime,
    has_goal: bool = False,
) -> RunRecord:
    terminal = state.terminal
    status = terminal if terminal else "running"
    failure_class = state.failure.failure_class if state.failure is not None else None
    route_log = [
        {
            "attempt_no": e.attempt_no,
            "node": e.node,
            "route": e.route,
            "reason": e.reason,
        }
        for e in state.route_log
    ]
    cost = None
    try:
        cost = float(state.spent.cost_usd)
    except Exception:  # noqa: BLE001
        cost = None
    return RunRecord(
        run_id=run_id,
        request=request,
        task_class=task_class,
        status=status,
        created_at=created_at,
        tenant_id=tenant_id,
        terminal=terminal,
        failure_class=failure_class,
        attempt_no=state.attempt_no,
        arm=state.arm,
        route_log=route_log,
        has_goal=has_goal,
        cost_usd=cost,
        criteria_hash=getattr(getattr(state, "manifest", None), "criteria_hash", None),
    )


def _load_from_checkpoints(root: Path, tenant_id: str, run_id: str) -> RunRecord | None:
    orch = GraphOrchestrator(root / "runs" / tenant_id)
    try:
        latest = orch.checkpoints.latest(run_id)
        if latest is None:
            return None
        _, _, _, state = latest
        return _record_from_state(
            run_id=run_id,
            request=state.task.request,
            task_class=state.task.task_class or "repo-chore",
            tenant_id=tenant_id,
            state=state,
            created_at=datetime.now(timezone.utc),
            has_goal=state.task.goal is not None,
        )
    finally:
        orch.close()
