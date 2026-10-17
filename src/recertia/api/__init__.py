"""FastAPI surface: health, runs (GraphOrchestrator), metrics dashboard, blob digests.

``POST /v1/runs`` executes offline via ``GraphOrchestrator.start`` (same path as
``recertia run``), not an enqueue-only stub. Optional CLI-parity fields: ``goal``,
``task_class`` (falls back to ``goal.task_class``, then ``repo-chore``), ``criteria``,
``script``, ``budget``, ``workdir``, ``arm``.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from contracts.criteria import TaskCriterion
from recertia.api.auth import ApiKeyStore, Principal, require_scope, validate_tenant_id
from recertia.api.console_routes import ConsoleContext, register_console_routes
from recertia.api.errors import V1HTTPError, envelope_http_exception, v1_error
from recertia.api.quotas import QuotaExceeded, QuotaStore
from recertia.api.remaining_routes import register_remaining_routes
from recertia.api.runs_models import RunCreate as RunCreate
from recertia.api.runs_models import RunRecord
from recertia.bootstrap import build_default_orchestrator as build_default_orchestrator
from recertia.config import ModelConfig
from recertia.graph.engine import GraphOrchestrator
from recertia.graph.store import CheckpointStore
from recertia.ids import InvalidIdError, validate_run_id
from recertia.paths import HostRootError, looks_absolute, resolve_under_host_root
from recertia.store.blobs import FilesystemBlobStore, normalize_blob_digest
from recertia.telemetry import get_telemetry, render_dashboard
from recertia.workspaces.registry import WorkspaceRegistry

_log = logging.getLogger("recertia.api")

DEFAULT_ROOT = Path(".recertia")


def _max_blob_bytes() -> int:
    from recertia.settings import load_settings

    return load_settings().max_blob_bytes


def _api_max_criterion_timeout_s() -> int:
    from recertia.settings import load_settings

    return load_settings().api_max_criterion_timeout_s


def _max_in_flight_runs() -> int:
    from recertia.settings import load_settings

    return load_settings().api_max_in_flight_runs


def _readiness(root: Path) -> tuple[bool, dict[str, object]]:
    """SQLite writability, free disk, and an optional worker heartbeat.

    A missing heartbeat is ignored. A present file older than 60s is not ready.
    """

    checks: dict[str, object] = {}
    ok = True
    try:
        from recertia.persistence.db import open_db

        conn = open_db(root / "quotas.sqlite")
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()
        checks["sqlite"] = "ok"
    except Exception:
        _log.exception("readiness sqlite check failed")
        ok = False
        checks["sqlite"] = "error"
    try:
        free = shutil.disk_usage(root).free
        checks["disk_free_bytes"] = free
        if free < 4096:
            ok = False
            checks["disk"] = "low"
        else:
            checks["disk"] = "ok"
    except OSError:
        ok = False
        checks["disk"] = "error"
        checks["disk_free_bytes"] = 0
    heartbeat = root / "worker.heartbeat"
    if not heartbeat.is_file():
        checks["worker"] = "absent"
    else:
        age = time.time() - heartbeat.stat().st_mtime
        checks["worker_heartbeat_age_s"] = round(age, 3)
        if age > 60:
            ok = False
            checks["worker"] = "stale"
        else:
            checks["worker"] = "ok"
    checks["status"] = "ok" if ok else "not_ready"
    return ok, checks


def _validate_run_id(run_id: str) -> str:
    try:
        return validate_run_id(run_id)
    except InvalidIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _tenant_has_checkpoints(root: Path, tenant_id: str, run_id: str) -> bool:
    """True when durable checkpoints already exist for this tenant's run id."""

    db = root / "runs" / tenant_id / "checkpoints.db"
    if not db.is_file():
        return False
    store = CheckpointStore(db)
    try:
        return store.latest_seq(run_id) is not None
    finally:
        store.close()


def _clamp_criteria_for_api(criteria: list[TaskCriterion]) -> list[TaskCriterion]:
    clamped: list[TaskCriterion] = []
    for criterion in criteria:
        if criterion.timeout_s > _api_max_criterion_timeout_s():
            clamped.append(
                criterion.model_copy(update={"timeout_s": _api_max_criterion_timeout_s()})
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


def create_app(
    *,
    root: Path | None = None,
    skills_root: Path | None = None,
    facts_root: Path | None = None,
    settings: object | None = None,
) -> FastAPI:
    from recertia.settings import Settings, load_settings

    loaded = settings if isinstance(settings, Settings) else load_settings()
    loaded.validate_deployment(role="api")
    from recertia.observability.logging import (
        bind_log_context,
        configure_logging,
        reset_log_context,
    )

    configure_logging(loaded)
    root = root or DEFAULT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    from recertia.persistence.root_lock import RootLock

    root_lock = RootLock(root / ".lock")
    root_lock.acquire_shared()
    from recertia.persistence.migrate_run_events import migrate_run_events

    migrate_run_events(root)
    skills_root = Path(skills_root) if skills_root is not None else Path("skills")
    facts_root = Path(facts_root) if facts_root is not None else Path("facts")
    key_store = ApiKeyStore(root / "api_keys.sqlite")
    quota_store = QuotaStore(root / "quotas.sqlite")
    # A previous process may have crashed after admit. Nothing is in flight now.
    quota_store.reset_inflight()
    workspace_registry = WorkspaceRegistry(root / "workspaces_registry.sqlite")
    blobs_by_tenant: dict[str, FilesystemBlobStore] = {}
    # Keyed by (tenant_id, run_id) so tenants cannot collide on run_id.
    runs: dict[tuple[str, str], RunRecord] = {}
    from recertia.app.runs import RunService

    run_service = RunService(
        quota_store,
        runs,
        lambda tenant_id, run_id: _load_from_checkpoints(root, tenant_id, run_id),
    )
    run_slots = threading.Semaphore(_max_in_flight_runs())
    app = FastAPI(title="Recertia", version="0.1.0")

    @app.middleware("http")
    async def _request_log_context(request: Any, call_next: Any) -> Any:
        raw = str(request.headers.get("x-request-id", ""))
        safe = raw and len(raw) <= 128 and raw.isascii() and "\n" not in raw and "\r" not in raw
        request_id = raw if safe else uuid4().hex
        tokens = bind_log_context(request_id=request_id)
        try:
            response = await call_next(request)
        finally:
            reset_log_context(tokens)
        response.headers["x-request-id"] = request_id
        return response

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
        run_service=run_service,
    )
    register_console_routes(app, console_ctx)
    register_remaining_routes(app, console_ctx)

    @app.exception_handler(V1HTTPError)
    async def _v1_http_error(_request: Any, exc: V1HTTPError) -> Any:
        return exc.response()

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Any, exc: StarletteHTTPException) -> Any:
        return envelope_http_exception(request, exc)

    @app.exception_handler(Exception)
    async def _unhandled(_request: Any, exc: Exception) -> JSONResponse:
        _log.error("unhandled error", exc_info=exc)
        return JSONResponse(status_code=500, content={"error": "internal error"})

    console_dir = Path(__file__).resolve().parents[3] / "console" / "static"
    if console_dir.is_dir():
        app.mount("/console/assets", StaticFiles(directory=console_dir), name="console-assets")

        @app.get("/console")
        @app.get("/console/")
        def console_index() -> FileResponse:
            return FileResponse(console_dir / "index.html")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> JSONResponse:
        ok, body = _readiness(root)
        return JSONResponse(status_code=200 if ok else 503, content=body)

    from recertia.api.runs_routes import register_run_routes

    register_run_routes(app, console_ctx)


    @app.post("/v1/blobs")
    def put_blob(
        payload: dict[str, Any],
        principal: Principal = Depends(require_scope("blobs", key_store)),
    ) -> dict[str, str]:
        blobs = blobs_by_tenant.setdefault(
            principal.tenant_id, FilesystemBlobStore(_tenant_blob_root(root, principal.tenant_id))
        )
        data = str(payload.get("data", "")).encode()
        if len(data) > _max_blob_bytes():
            raise HTTPException(
                status_code=413,
                detail=f"blob exceeds max size of {_max_blob_bytes()} bytes",
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
    app.state.root_lock = root_lock
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
