"""Run admission, lookup, and listing. Routes do not call the quota store."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from uuid import uuid4

from contracts.budget import Budget
from contracts.criteria import TaskCriterion
from recertia.app.authz import Resource
from recertia.errors import ConfigError, NotFound
from recertia.graph.lease import RunBusy
from recertia.persistence.layout import RuntimeLayout
from recertia.ports.identity import Principal
from recertia.runtime.errors import RunIdExists, WorkspaceBusy
from recertia.runtime.registry import RunRegistry
from recertia.runtime.workspace_lock import WorkspaceLock, WorkspaceLockHandle
from recertia.solver.container import ensure_api_execution_ready

_WAIT_CAP_S = 60.0


class RunQuota(Protocol):
    def admit(
        self,
        tenant_id: str,
        *,
        quota: Any = None,
        inflight: int | None = None,
        depth: int | None = None,
        max_queue_depth: int | None = None,
        max_in_flight: int | None = None,
    ) -> None: ...

    def release_inflight(self, tenant_id: str) -> None: ...

    def complete(self, tenant_id: str, *, cost_usd: float = 0.0) -> None: ...


@dataclass(frozen=True)
class AcceptedRun:
    """HTTP 202 while the run is queued. Sync callers wait unless this is returned."""

    body: dict[str, Any]
    status_code: int = 202
    headers: dict[str, str] | None = None


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
    *,
    mode: str = "sync",
    default_async: bool = False,
    sync_wait_s: float = _WAIT_CAP_S,
) -> float | None:
    """Seconds to block after enqueue, or None to return 202 immediately.

    D4: ``RECERTIA_API_DEFAULT_ASYNC=0`` waits when ``mode`` is omitted or
    ``sync``. ``mode=async`` returns 202 unless a wait header/query is set.
    """

    cap = max(sync_wait_s, 0.0)
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
                return cap
            if lowered.startswith("wait="):
                raw = token.split("=", 1)[1].strip()
                break
    if raw is not None:
        text = raw.strip()
        if text == "":
            return cap
        try:
            seconds = float(text)
        except ValueError as exc:
            raise ConfigError("wait must be seconds") from exc
        if seconds < 0:
            raise ConfigError("wait must be seconds")
        if seconds == 0:
            return None
        return min(seconds, cap)
    if mode == "async":
        return None
    return cap


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
        queue = None
        try:
            queue = self._ctx().queue
        except RuntimeError:
            queue = None
        if queue is None:
            self.quota.admit(tenant_id)
            return
        self.quota.admit(
            tenant_id,
            inflight=queue.inflight(tenant_id),
            depth=queue.depth(tenant_id),
            max_queue_depth=queue.max_queue_depth(tenant_id),
            max_in_flight=queue.max_in_flight(tenant_id),
        )

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
        """List runs for a tenant (REC-056f).

        Order is ``created_at DESC, run_id DESC``. The cursor is an opaque
        base64 JSON keyset. A malformed cursor raises ``InvalidCursor``.
        Runs that exist only in the checkpoint store (pre-migration) are
        still included.
        """
        from recertia.runtime.cursor import decode_cursor, encode_cursor
        from recertia.runtime.registry import RunRegistry

        created_at_before = run_id_before = None
        if cursor:
            created_at_before, run_id_before = decode_cursor(cursor)

        chosen: dict[str, dict[str, Any]] = {}
        # Checkpoint rows (pre-migration runs that are not in the registry).
        from recertia.graph.store import CheckpointStore
        from recertia.persistence.import_legacy import live_checkpoint_db

        db = live_checkpoint_db(Path(runs_root))
        if db is not None:
            store = CheckpointStore(db)
            try:
                for row in store.list_summaries(limit=10_000):
                    rid = str(row["run_id"])
                    if rid not in chosen:
                        chosen[rid] = {**row, "tenant_id": tenant_id}
            finally:
                store.close()
        # Registry rows (summary_json) override checkpoint rows.
        try:
            from recertia.persistence.layout import RuntimeLayout
            reg_path = RuntimeLayout(Path(runs_root)).local_runs().runtime_db
            if reg_path.is_file():
                reg = RunRegistry(reg_path)
                try:
                    for row in reg.list_page(
                        tenant_id,
                        limit=limit + 1,
                        created_at_before=created_at_before,
                        run_id_before=run_id_before,
                    ):
                        chosen[str(row["run_id"])] = {**row, "tenant_id": tenant_id}
                finally:
                    reg.close()
        except Exception:
            pass
        # In-memory runs not yet flushed.
        for (tid, run_id), rec in self.runs.items():
            if tid != tenant_id or rec is None:
                continue
            if created_at_before is not None:
                rec_created = str(getattr(rec, "created_at", "") or "")
                if (rec_created, run_id) >= (created_at_before, run_id_before or ""):
                    continue
            if task_class and getattr(rec, "task_class", None) != task_class:
                continue
            if terminal and (getattr(rec, "terminal", None) or "") != terminal:
                continue
            chosen[run_id] = _summary_from_record(rec)
        ordered = sorted(
            chosen.values(),
            key=lambda row: (str(row.get("created_at", "")), str(row["run_id"])),
            reverse=True,
        )
        has_more = len(ordered) > limit
        items = ordered[:limit]
        if items and has_more:
            last = items[-1]
            next_cursor = encode_cursor(str(last.get("created_at", "")), str(last["run_id"]))
        else:
            next_cursor = None
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

            handle = ws_handle
            ws_handle = None
            if handle is not None:
                handle.release()
            return self._enqueue_run(
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
                ws_handle=None,
                action="start",
            )
        finally:
            if ws_handle is not None:
                ws_handle.release()

    def _enqueue_run(
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
        action: str = "start",
        replace_terminal: bool = False,
    ) -> Any:
        ctx = self._ctx()
        tenant_id = principal.tenant_id
        mode = getattr(body, "mode", None) or "sync"
        settings = getattr(ctx, "settings", None)
        try:
            fields: set[str] = set(getattr(body, "model_fields_set", set()))
            explicit_mode = "mode" in fields
            wait_mode = mode
            if (
                bool(getattr(settings, "api_default_async", False))
                and not explicit_mode
                and mode != "async"
            ):
                wait_mode = "async"
            wait_s = wait_seconds(
                wait,
                x_recertia_wait,
                prefer,
                mode=wait_mode,
                default_async=bool(getattr(settings, "api_default_async", False)),
                sync_wait_s=float(getattr(settings, "api_sync_wait_s", _WAIT_CAP_S)),
            )
            self.admit(tenant_id)
            self.runs[run_key] = ctx.queued_run_record(
                run_id=run_id,
                request=request,
                task_class=task_class,
                status="queued",
                created_at=datetime.now(timezone.utc),
                tenant_id=tenant_id,
                terminal=None,
                has_goal=getattr(body, "goal", None) is not None,
                mode=mode,
                workspace_id=getattr(body, "workspace_id", None),
                workdir=str(workdir),
            )
            ctx.events.append(tenant_id, run_id, "run.queued", {"mode": mode, "action": action})
            try:
                ctx.enqueue_async_run(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    goal=getattr(body, "goal", None),
                    request=request,
                    task_class=getattr(body, "task_class", None) or task_class,
                    criteria=criteria,
                    budget=budget,
                    workdir=workdir,
                    script=getattr(body, "script", None),
                    arm=getattr(body, "arm", None) or "treatment",
                    approve_tools=approve_tools,
                    skills_root=ctx.tenant_skills_root(tenant_id),
                    facts_root=ctx.facts_root,
                    runs_root=ctx.root / "runs" / tenant_id,
                    index_path=ctx.root / "runs" / tenant_id / "skill_index.db",
                    model_config=model_config,
                    max_criterion_timeout_s=ctx.max_criterion_timeout_s(),
                    mode=mode,
                    action=action,
                    replace_terminal=replace_terminal,
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
                    if finished.error_code == "run_busy":
                        raise RunBusy(f"run {run_id} is busy")
                    if finished.error_code == "workspace_busy":
                        raise WorkspaceBusy(f"workspace busy: {workdir}")
                    deadline = time.monotonic() + 2.0
                    while time.monotonic() < deadline:
                        loaded = self.get(tenant_id, run_id)
                        if loaded is not None and getattr(loaded, "terminal", None):
                            return loaded
                        disk = self.load_run(tenant_id, run_id)
                        if disk is not None and getattr(disk, "terminal", None):
                            self.runs[run_key] = disk
                            return disk
                        time.sleep(0.01)
            retry_after = str(int(getattr(settings, "api_sync_wait_s", 30) or 30))
            return AcceptedRun(
                {
                    "run_id": run_id,
                    "status": "queued",
                    "mode": mode,
                    "tenant_id": tenant_id,
                    "task_class": task_class,
                    "workspace_id": getattr(body, "workspace_id", None),
                    "workdir": str(workdir),
                },
                headers={"Retry-After": retry_after},
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
        row = ctx.queue.get(tenant_id, run_id)
        if row is not None and row.status in {"queued", "claimed", "running"}:
            raise RunBusy(f"run {run_id} is {row.status}")
        workdir = ctx.load_persisted_workdir(tenant_id, run_id)
        if workdir is None:
            workdir = ctx.canonical_run_workdir(ctx.root, tenant_id, run_id)
        if not workdir.exists():
            raise NotFound("run workdir not found")
        owner_id = f"{tenant_id}/{run_id}"
        ws_handle = _acquire_workspace(ctx.root, workdir, owner_id=owner_id)
        ws_handle.release()
        existing = self.runs.get(run_key)
        from types import SimpleNamespace

        body = SimpleNamespace(
            mode="sync",
            goal=None,
            task_class=getattr(existing, "task_class", None) if existing else None,
            script=None,
            arm="treatment",
            workspace_id=getattr(existing, "workspace_id", None) if existing else None,
        )
        return self._enqueue_run(
            principal,
            body,
            run_id=run_id,
            run_key=run_key,
            request=getattr(existing, "request", None) if existing else None,
            task_class=(getattr(existing, "task_class", None) if existing else None)
            or "repo-chore",
            criteria=[],
            budget=Budget(),
            workdir=workdir,
            approve_tools=ctx.principal_may_exec(principal),
            model_config=None,
            wait=None,
            x_recertia_wait=None,
            prefer=None,
            ws_handle=None,
            action="resume",
            replace_terminal=True,
        )

    def cancel(self, principal: Principal, run_id: str) -> dict[str, Any]:
        ctx = self._ctx()
        tenant_id = principal.tenant_id
        run_id = ctx.validate_run_id(run_id)
        self._require(principal, "runs.cancel", tenant_id=tenant_id)
        ctx.queue.request_cancel(tenant_id, run_id, by=principal.key_id)
        worker = getattr(ctx, "worker", None)
        if worker is not None:
            worker.request_cancel(tenant_id, run_id)
        ctx.events.append(tenant_id, run_id, "run.cancelled", {"by": principal.key_id})
        return {"run_id": run_id, "status": "cancel_requested"}
