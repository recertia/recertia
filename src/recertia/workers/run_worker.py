"""In-process async run worker (console C2).

``serve`` is the process entry composition root. It loads ``Settings`` and
refuses an unsafe ``multi_node`` configuration before any run is claimed.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, ClassVar
from weakref import WeakSet

from contracts.budget import Budget
from contracts.common import Arm
from contracts.criteria import TaskCriterion
from contracts.goal import Goal
from contracts.run import Task
from recertia.api.events import RunEventLog
from recertia.bootstrap import build_default_orchestrator, resolve_task_class
from recertia.config import ModelConfig
from recertia.graph.hooks import RunCancelled
from recertia.graph.lease import RunBusy
from recertia.ports.identity import Principal
from recertia.runtime.errors import WorkspaceBusy


@dataclass
class AsyncRunRequest:
    run_id: str
    tenant_id: str
    goal: Goal | None
    request: str | None
    task_class: str | None
    criteria: list[TaskCriterion]
    budget: Budget
    workdir: Path
    script: list[str] | None
    arm: Arm
    approve_tools: bool
    skills_root: Path
    facts_root: Path
    runs_root: Path
    index_path: Path
    model_config: ModelConfig | None = None
    max_criterion_timeout_s: int | None = None
    cancel_check: Callable[[], bool] | None = None
    mode: str = "async"
    action: str = "start"


def dump_async_request(req: AsyncRunRequest) -> dict[str, Any]:
    """JSON payload stored on ``run_queue``. The cancel callback is not persisted."""

    return {
        "run_id": req.run_id,
        "tenant_id": req.tenant_id,
        "goal": req.goal.model_dump(mode="json") if req.goal is not None else None,
        "request": req.request,
        "task_class": req.task_class,
        "criteria": [item.model_dump(mode="json") for item in req.criteria],
        "budget": req.budget.model_dump(mode="json"),
        "workdir": str(req.workdir),
        "script": req.script,
        "arm": req.arm,
        "approve_tools": req.approve_tools,
        "skills_root": str(req.skills_root),
        "facts_root": str(req.facts_root),
        "runs_root": str(req.runs_root),
        "index_path": str(req.index_path),
        "model_config": asdict(req.model_config) if req.model_config is not None else None,
        "max_criterion_timeout_s": req.max_criterion_timeout_s,
        "mode": req.mode,
        "action": req.action,
    }


def load_async_request(payload: dict[str, Any]) -> AsyncRunRequest:
    from contracts.goal import Goal

    goal_raw = payload.get("goal")
    budget_raw = payload.get("budget")
    model_raw = payload.get("model_config")
    criteria_raw = payload.get("criteria") or []
    script_raw = payload.get("script")
    script: list[str] | None
    if isinstance(script_raw, list) and all(isinstance(part, str) for part in script_raw):
        script = list(script_raw)
    else:
        script = None
    raw_arm = payload.get("arm")
    chosen: Arm = "treatment"
    if raw_arm == "control":
        chosen = "control"
    elif raw_arm == "shadow":
        chosen = "shadow"
    elif raw_arm == "practice":
        chosen = "practice"
    return AsyncRunRequest(
        run_id=str(payload.get("run_id") or ""),
        tenant_id=str(payload.get("tenant_id") or ""),
        goal=Goal.model_validate(goal_raw) if isinstance(goal_raw, dict) else None,
        request=payload.get("request") if isinstance(payload.get("request"), str) else None,
        task_class=payload.get("task_class") if isinstance(payload.get("task_class"), str) else None,
        criteria=[TaskCriterion.model_validate(item) for item in criteria_raw],
        budget=Budget.model_validate(budget_raw) if isinstance(budget_raw, dict) else Budget(),
        workdir=Path(str(payload.get("workdir") or ".")),
        script=script,
        arm=chosen,
        approve_tools=bool(payload.get("approve_tools")),
        skills_root=Path(str(payload.get("skills_root") or "skills")),
        facts_root=Path(str(payload.get("facts_root") or "facts")),
        runs_root=Path(str(payload.get("runs_root") or ".recertia/runs")),
        index_path=Path(str(payload.get("index_path") or ".recertia/skill_index.db")),
        model_config=ModelConfig(**model_raw) if isinstance(model_raw, dict) else None,
        max_criterion_timeout_s=payload.get("max_criterion_timeout_s")
        if isinstance(payload.get("max_criterion_timeout_s"), int)
        else None,
        mode=str(payload.get("mode") or "async"),
        action=str(payload.get("action") or "start"),
    )


class AsyncRunWorker:
    _live: ClassVar[WeakSet[AsyncRunWorker]] = WeakSet()

    def __init__(
        self,
        *,
        events: RunEventLog,
        on_complete: Callable[[str, str, Any], None] | None = None,
        on_failed: Callable[..., None] | None = None,
        max_workers: int = 2,
    ) -> None:
        self.events = events
        self.on_complete = on_complete
        self.on_failed = on_failed
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="recertia-run")
        self._cancel: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._futures: dict[tuple[str, str], Future[Any]] = {}
        self._queue: Any = None
        type(self)._live.add(self)

    def attach_queue(self, queue: Any) -> None:
        """Forward cancel onto the durable queue. The pool remains the shim."""

        self._queue = queue

    def shutdown(self, wait: bool = False) -> None:
        self._pool.shutdown(wait=wait, cancel_futures=True)

    def request_cancel(self, tenant_id: str, run_id: str) -> None:
        with self._lock:
            self._cancel.add((tenant_id, run_id))
        queue = self._queue
        if queue is not None:
            queue.request_cancel(tenant_id, run_id)

    def is_cancelled(self, tenant_id: str, run_id: str) -> bool:
        with self._lock:
            return (tenant_id, run_id) in self._cancel

    def submit(self, req: AsyncRunRequest) -> None:
        self.events.append(req.tenant_id, req.run_id, "run.queued", {"mode": "async"})
        fut = self._pool.submit(self._run, req)
        with self._lock:
            self._futures[(req.tenant_id, req.run_id)] = fut

    def _fail(self, req: AsyncRunRequest, *, cancelled: bool = False) -> None:
        if self.on_failed is not None:
            self.on_failed(req.tenant_id, req.run_id, cancelled=cancelled)

    def _run(self, req: AsyncRunRequest) -> str:
        self.events.append(
            req.tenant_id,
            req.run_id,
            "run.started",
            {"task_class": req.task_class or "repo-chore", "mode": "async"},
        )
        if self.is_cancelled(req.tenant_id, req.run_id) or self._cancel_flag(req):
            self.events.append(req.tenant_id, req.run_id, "run.cancelled", {"by": "operator"})
            self._fail(req, cancelled=True)
            return "cancelled"
        task_class = resolve_task_class(
            explicit=req.task_class,
            goal_task_class=req.goal.task_class if req.goal else None,
        )
        request = req.request
        if req.goal is not None and req.goal.context and not request:
            request = req.goal.context
        task = Task(
            task_id=req.run_id,
            goal=req.goal,
            request=request,
            task_class=task_class,
            submitted_at=datetime.now(timezone.utc),
            submitted_by="async-worker",
        )
        from recertia.paths import tenant_skills_root

        scopes = frozenset({"runs", "exec"}) if req.approve_tools else frozenset({"runs"})
        skills = tenant_skills_root(
            control_root=req.runs_root.parent.parent,
            shared=req.skills_root,
            tenant_id=req.tenant_id,
        )
        worker_principal = Principal.from_api_key(
            key_id="async-worker", tenant_id=req.tenant_id, scopes=scopes
        )
        bundle = build_default_orchestrator(
            req.runs_root,
            skills_root=skills,
            facts_root=req.facts_root,
            index_path=req.index_path,
            approve_default_tools=req.approve_tools,
            principal=worker_principal,
            surface="worker",
            model_config=req.model_config,
        )
        bundle.orchestrator.max_criterion_timeout_s = req.max_criterion_timeout_s
        ws_handle = None
        if req.cancel_check is not None:
            from recertia.graph.hooks import CancelHopHook

            bundle.orchestrator.hooks = [
                CancelHopHook(req.cancel_check),
                *bundle.orchestrator.hooks,
            ]
        try:
            from recertia.persistence.layout import RuntimeLayout
            from recertia.runtime.workspace_lock import WorkspaceLock

            owner_id = f"{req.tenant_id}/{req.run_id}"
            control_root = req.runs_root.parent.parent
            ws_handle = WorkspaceLock(RuntimeLayout(control_root).locks_dir).acquire(
                req.workdir, owner_id=owner_id
            )
            if self.is_cancelled(req.tenant_id, req.run_id) or self._cancel_flag(req):
                self.events.append(req.tenant_id, req.run_id, "run.cancelled", {"by": "operator"})
                self._fail(req, cancelled=True)
                return "cancelled"
            from recertia.graph.engine import RunIdExistsError

            try:
                if req.action == "resume":
                    state = bundle.orchestrator.resume(
                        req.run_id, workdir=req.workdir, script=req.script
                    )
                else:
                    try:
                        state = bundle.orchestrator.start(
                            req.run_id,
                            task,
                            req.criteria,
                            budget=req.budget,
                            workdir=req.workdir,
                            script=req.script,
                            arm=req.arm,
                            manifest=bundle.run_manifest(),
                        )
                    except RunIdExistsError:
                        state = bundle.orchestrator.resume(
                            req.run_id, workdir=req.workdir, script=req.script
                        )
            except RunCancelled:
                raise
            self.events.append(
                req.tenant_id,
                req.run_id,
                "run.finished",
                {
                    "terminal": state.terminal,
                    "cost_usd": state.spent.cost_usd,
                    "attempt_no": state.attempt_no,
                },
            )
            if self.on_complete is not None:
                self.on_complete(req.tenant_id, req.run_id, state)
            return "succeeded"
        except RunCancelled:
            self.events.append(req.tenant_id, req.run_id, "run.cancelled", {"by": "operator"})
            self._fail(req, cancelled=True)
            return "cancelled"
        except RunBusy:
            return "run_busy"
        except WorkspaceBusy:
            return "workspace_busy"
        except Exception as exc:  # noqa: BLE001
            self.events.append(
                req.tenant_id,
                req.run_id,
                "error",
                {"code": "run_failed", "message": str(exc)[:500], "retryable": False},
            )
            self.events.append(
                req.tenant_id,
                req.run_id,
                "run.finished",
                {"terminal": "error", "cost_usd": 0.0, "attempt_no": 0},
            )
            self._fail(req, cancelled=False)
            return "failed"
        finally:
            if ws_handle is not None:
                ws_handle.release()
            bundle.close()

    def _cancel_flag(self, req: AsyncRunRequest) -> bool:
        if req.cancel_check is None:
            return False
        return bool(req.cancel_check())


def serve() -> None:
    """Worker composition root. Validates deployment, then returns (in-process pool)."""

    from recertia.errors import RecertiaError
    from recertia.settings import load_settings

    settings = load_settings()
    try:
        settings.validate_deployment(role="worker")
    except RecertiaError:
        raise
    return None
