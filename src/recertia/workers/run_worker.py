"""In-process async run worker (console C2).

``serve`` is the process entry composition root. It loads ``Settings`` and
refuses an unsafe ``multi_node`` configuration before any run is claimed.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from contracts.budget import Budget
from contracts.common import Arm
from contracts.criteria import TaskCriterion
from contracts.goal import Goal
from contracts.run import Task
from recertia.api.events import RunEventLog
from recertia.bootstrap import build_default_orchestrator, resolve_task_class
from recertia.config import ModelConfig
from recertia.ports.identity import Principal


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


class AsyncRunWorker:
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

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def request_cancel(self, tenant_id: str, run_id: str) -> None:
        with self._lock:
            self._cancel.add((tenant_id, run_id))

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

    def _run(self, req: AsyncRunRequest) -> None:
        self.events.append(
            req.tenant_id,
            req.run_id,
            "run.started",
            {"task_class": req.task_class or "repo-chore", "mode": "async"},
        )
        if self.is_cancelled(req.tenant_id, req.run_id):
            self.events.append(req.tenant_id, req.run_id, "run.cancelled", {"by": "operator"})
            self._fail(req, cancelled=True)
            return
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
        try:
            # Cooperative cancel is checked via cancel set; full mid-node cancel lands later.
            if self.is_cancelled(req.tenant_id, req.run_id):
                self.events.append(req.tenant_id, req.run_id, "run.cancelled", {"by": "operator"})
                self._fail(req, cancelled=True)
                return
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
        finally:
            bundle.close()


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
