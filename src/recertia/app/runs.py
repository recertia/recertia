"""Run admission, lookup, and listing. Routes do not call the quota store."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from uuid import uuid4

from contracts.budget import Budget
from contracts.criteria import TaskCriterion
from contracts.run import Task
from recertia.app.authz import Resource
from recertia.errors import ConfigError, NotFound, RateLimited, RecertiaError
from recertia.graph.engine import RunIdExistsError
from recertia.graph.lease import RunBusy
from recertia.persistence.layout import RuntimeLayout
from recertia.ports.identity import Principal
from recertia.runtime.errors import RunIdExists
from recertia.runtime.registry import RunRegistry
from recertia.runtime.workspace_lock import WorkspaceLock, WorkspaceLockHandle
from recertia.solver.container import ensure_api_execution_ready
from recertia.telemetry import get_telemetry

_log = logging.getLogger("recertia.api")
_WAIT_CAP_S = 30.0


class RunQuota(Protocol):
    def admit(self, tenant_id: str, *, quota: Any = None) -> None: ...

    def release_inflight(self, tenant_id: str) -> None: ...

    def complete(self, tenant_id: str, *, cost_usd: float = 0.0) -> None: ...


@dataclass(frozen=True)
class AcceptedRun:
    """Async accept (HTTP 202) until REL-4 makes this the default."""

    body: dict[str, Any]
    status_code: int = 202


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


def _create_request_hash(body: Any, run_id: str) -> str:
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
    body: Any,
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
    finally:
        registry.close()


def _acquire_workspace(
    root: Path,
    workdir: Path,
    *,
    owner_id: str,
) -> WorkspaceLockHandle:
    return WorkspaceLock(RuntimeLayout(root).locks_dir).acquire(workdir, owner_id=owner_id)


def wait_seconds(
    wait: str | None,
    x_recertia_wait: str | None,
    prefer: str | None,
) -> float | None:
    """Optional enqueue-and-wait budget. Absent means return 202 immediately."""

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
        raise ConfigError("wait must be seconds") from exc
    if seconds < 0:
        raise ConfigError("wait must be seconds")
    return min(seconds, _WAIT_CAP_S)


class RunService:
    """create/resume admit here. get and list read the same cache the routes use."""

    def __init__(
        self,
        quota: RunQuota,
        runs: dict[tuple[str, str], Any],
        load_run: Callable[[str, str], Any | None],
        *,
        pdp: Any | None = None,
    ) -> None:
        self.quota = quota
        self.runs = runs
        self.load_run = load_run
        self._pdp = pdp
        self._host: Any | None = None

    def bind(self, host: Any) -> None:
        """Attach AppState (workdir, queue, events). Called from ``create_app``."""

        self._host = host
        if getattr(host, "pdp", None) is not None:
            self._pdp = host.pdp

    def _ctx(self) -> Any:
        if self._host is None:
            raise RuntimeError("RunService.bind() was not called")
        return self._host

    def _require(self, principal: Principal, action: str, *, tenant_id: str) -> None:
        pdp = self._pdp
        if pdp is None:
            raise RuntimeError("RunService has no PolicyDecisionPoint")
        pdp.require(principal, action, Resource(tenant_id=tenant_id))

    def admit(self, tenant_id: str) -> None:
        self.quota.admit(tenant_id)

    def release(self, tenant_id: str) -> None:
        self.quota.release_inflight(tenant_id)

    def complete(self, tenant_id: str, *, cost_usd: float = 0.0) -> None:
        self.quota.complete(tenant_id, cost_usd=cost_usd)

    def get(
        self,
        tenant_id: str,
        run_id: str,
        *,
        principal: Principal | None = None,
    ) -> Any | None:
        if principal is not None:
            self._require(principal, "runs.read", tenant_id=tenant_id)
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

    def create(
        self,
        principal: Principal,
        body: Any,
        *,
        wait: str | None = None,
        x_recertia_wait: str | None = None,
        prefer: str | None = None,
    ) -> Any:
        """Admit, reserve, and start or enqueue. The HTTP route does not drive the engine."""

        ctx = self._ctx()
        ensure_api_execution_ready()
        tenant_id = principal.tenant_id
        self._require(principal, "runs.create", tenant_id=tenant_id)
        if body.script:
            self._require(principal, "runs.script", tenant_id=tenant_id)

        model_config = ctx.model_config_for_run(body.model)
        run_id = ctx.validate_run_id(body.run_id or f"run-{uuid4().hex[:12]}")
        run_key = (tenant_id, run_id)
        if run_key in self.runs or ctx.tenant_has_checkpoints(tenant_id, run_id):
            raise RunIdExists("run_id already exists")

        workdir, wd_meta = ctx.resolve_create_workdir(
            ctx.root,
            tenant_id,
            run_id,
            body.workdir,
            workspace_id=body.workspace_id,
            registry=ctx.workspace_registry,
        )
        _reserve_run_id(
            ctx.root,
            tenant_id=tenant_id,
            run_id=run_id,
            body=body,
            workdir=workdir,
        )
        owner_id = f"{tenant_id}/{run_id}"
        ws_handle: WorkspaceLockHandle | None = _acquire_workspace(
            ctx.root, workdir, owner_id=owner_id
        )
        try:
            if wd_meta.get("kind") != "registered":
                workdir.mkdir(parents=True, exist_ok=True)
            ctx.persist_workdir(ctx.root, tenant_id, run_id, workdir, meta=wd_meta)

            request = body.request
            if body.goal is not None and body.goal.context and not request:
                request = body.goal.context

            task_class = ctx.resolve_task_class(
                explicit=body.task_class,
                goal_task_class=body.goal.task_class if body.goal else None,
            )
            criteria = ctx.clamp_criteria(
                [TaskCriterion.model_validate(item) for item in (body.criteria or [])]
            )
            budget = Budget.model_validate(body.budget) if body.budget else Budget()
            approve_tools = ctx.principal_may_exec(principal)

            if body.mode == "async":
                handle = ws_handle
                ws_handle = None
                return self._create_async(
                    principal,
                    body,
                    run_id=run_id,
                    run_key=run_key,
                    request=request,
                    task_class=task_class,
                    criteria=criteria,
                    budget=budget,
                    workdir=workdir,
                    approve_tools=approve_tools,
                    model_config=model_config,
                    wait=wait,
                    x_recertia_wait=x_recertia_wait,
                    prefer=prefer,
                    ws_handle=handle,
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
                tenant_id=tenant_id,
                run_id=run_id,
                task_class=task_class,
            )
            self.admit(tenant_id)
            if not ctx.run_slots.acquire(blocking=False):
                self.release(tenant_id)
                raise RateLimited("too many in-flight runs", run_id=run_id)

            bundle = ctx.orchestrator_factory()(
                ctx.root / "runs" / tenant_id,
                skills_root=ctx.tenant_skills_root(tenant_id),
                facts_root=ctx.facts_root,
                index_path=ctx.root / "runs" / tenant_id / "skill_index.db",
                approve_default_tools=approve_tools,
                principal=principal,
                surface="api",
                model_config=model_config,
            )
            bundle.orchestrator.max_criterion_timeout_s = ctx.max_criterion_timeout_s()
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
            except (RunIdExistsError, RunBusy):
                raise
            except Exception as exc:
                get_telemetry().emit(
                    "run.finished",
                    tenant_id=tenant_id,
                    run_id=run_id,
                    terminal="error",
                )
                _log.exception("create_run failed tenant_id=%s run_id=%s", tenant_id, run_id)
                raise RecertiaError("internal server error") from exc
            finally:
                bundle.close()
                ctx.run_slots.release()
                if succeeded:
                    self.complete(tenant_id, cost_usd=cost_usd)
                else:
                    self.release(tenant_id)

            rec = ctx.record_from_state(
                run_id=run_id,
                request=request,
                task_class=task_class,
                tenant_id=tenant_id,
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
            self.runs[run_key] = rec
            get_telemetry().emit(
                "run.finished",
                tenant_id=tenant_id,
                run_id=run_id,
                terminal=state.terminal,
            )
            return rec
        finally:
            if ws_handle is not None:
                ws_handle.release()

    def _create_async(
        self,
        principal: Principal,
        body: Any,
        *,
        run_id: str,
        run_key: tuple[str, str],
        request: str | None,
        task_class: str,
        criteria: Sequence[TaskCriterion],
        budget: Budget,
        workdir: Path,
        approve_tools: bool,
        model_config: Any,
        wait: str | None,
        x_recertia_wait: str | None,
        prefer: str | None,
        ws_handle: WorkspaceLockHandle | None,
    ) -> Any:
        ctx = self._ctx()
        tenant_id = principal.tenant_id
        try:
            wait_s = wait_seconds(wait, x_recertia_wait, prefer)
            self.admit(tenant_id)
            self.runs[run_key] = ctx.queued_run_record(
                run_id=run_id,
                request=request,
                task_class=task_class,
                status="queued",
                created_at=datetime.now(timezone.utc),
                tenant_id=tenant_id,
                terminal=None,
                has_goal=body.goal is not None,
                mode="async",
                workspace_id=body.workspace_id,
                workdir=str(workdir),
            )
            ctx.events.append(tenant_id, run_id, "run.queued", {"mode": "async"})
            try:
                ctx.enqueue_async_run(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    goal=body.goal,
                    request=request,
                    task_class=body.task_class,
                    criteria=criteria,
                    budget=budget,
                    workdir=workdir,
                    script=body.script,
                    arm=body.arm,
                    approve_tools=approve_tools,
                    skills_root=ctx.tenant_skills_root(tenant_id),
                    facts_root=ctx.facts_root,
                    runs_root=ctx.root / "runs" / tenant_id,
                    index_path=ctx.root / "runs" / tenant_id / "skill_index.db",
                    model_config=model_config,
                    max_criterion_timeout_s=ctx.max_criterion_timeout_s(),
                )
            except Exception:
                self.release(tenant_id)
                raise
            ctx.embedded.ensure_started()
            held = ws_handle
            ws_handle = None
            if held is not None:
                held.release()
            if wait_s is not None:
                finished = ctx.queue.wait(tenant_id, run_id, timeout_s=wait_s)
                if finished is not None:
                    loaded = self.get(tenant_id, run_id)
                    if loaded is not None and getattr(loaded, "terminal", None):
                        return loaded
            return AcceptedRun(
                {
                    "run_id": run_id,
                    "status": "queued",
                    "mode": "async",
                    "tenant_id": tenant_id,
                    "task_class": task_class,
                    "workspace_id": body.workspace_id,
                    "workdir": str(workdir),
                }
            )
        finally:
            if ws_handle is not None:
                ws_handle.release()

    def resume(self, principal: Principal, run_id: str) -> Any:
        ctx = self._ctx()
        ensure_api_execution_ready()
        tenant_id = principal.tenant_id
        self._require(principal, "runs.resume", tenant_id=tenant_id)
        run_id = ctx.validate_run_id(run_id)
        run_key = (tenant_id, run_id)
        workdir = ctx.load_persisted_workdir(tenant_id, run_id)
        if workdir is None:
            workdir = ctx.canonical_run_workdir(ctx.root, tenant_id, run_id)
        if not workdir.exists():
            raise NotFound("run workdir not found")
        owner_id = f"{tenant_id}/{run_id}"
        ws_handle = _acquire_workspace(ctx.root, workdir, owner_id=owner_id)
        try:
            self.admit(tenant_id)
            if not ctx.run_slots.acquire(blocking=False):
                self.release(tenant_id)
                raise RateLimited("too many in-flight runs", run_id=run_id)
            bundle = None
            try:
                bundle = ctx.orchestrator_factory()(
                    ctx.root / "runs" / tenant_id,
                    skills_root=ctx.tenant_skills_root(tenant_id),
                    facts_root=ctx.facts_root,
                    index_path=ctx.root / "runs" / tenant_id / "skill_index.db",
                    approve_default_tools=ctx.principal_may_exec(principal),
                    principal=principal,
                    surface="api",
                )
                bundle.orchestrator.max_criterion_timeout_s = ctx.max_criterion_timeout_s()
                state = bundle.orchestrator.resume(run_id, workdir=workdir)
            except RunBusy:
                raise
            except ValueError as exc:
                raise NotFound(str(exc)) from exc
            except Exception as exc:
                _log.exception("resume_run failed tenant_id=%s run_id=%s", tenant_id, run_id)
                raise RecertiaError("internal server error") from exc
            finally:
                if bundle is not None:
                    bundle.close()
                ctx.run_slots.release()
                self.release(tenant_id)

            existing = self.runs.get(run_key)
            rec = ctx.record_from_state(
                run_id=run_id,
                request=existing.request if existing else state.task.request,
                task_class=(
                    existing.task_class if existing else (state.task.task_class or "repo-chore")
                ),
                tenant_id=tenant_id,
                state=state,
                created_at=existing.created_at if existing else datetime.now(timezone.utc),
                has_goal=existing.has_goal if existing else (state.task.goal is not None),
            )
            self.runs[run_key] = rec
            return rec
        finally:
            ws_handle.release()
