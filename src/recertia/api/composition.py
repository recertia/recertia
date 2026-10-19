"""Assemble ``AppState`` for ``create_app``. Lives in the API layer so it may import workers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from recertia.api.console_auth import SessionStore
from recertia.api.events import RunEventLog
from recertia.api.identity import IdentityResolver
from recertia.api.jobs_store import JobRunStore
from recertia.app.authz import PolicyDecisionPoint
from recertia.app.checkpoints import CheckpointReader
from recertia.app.evals import EvalService
from recertia.app.jobs import JobService
from recertia.app.keys import KeyService
from recertia.app.library import LibraryService
from recertia.app.ops_service import OpsService
from recertia.app.programs import ProgramService
from recertia.app.promotion import PromotionService
from recertia.app.state import AppState
from recertia.app.workspaces import WorkspaceService
from recertia.programs.store import ProgramStore
from recertia.proposals.store import ProposalStore
from recertia.workers.embedded import EmbeddedQueueWorker
from recertia.workers.queue import QueueItem, open_run_queue
from recertia.workers.run_worker import AsyncRunRequest, AsyncRunWorker, dump_async_request


def assemble_app_state(
    *,
    root: Path,
    skills_root: Path,
    facts_root: Path,
    key_store: Any,
    quota_store: Any,
    runs: dict[tuple[str, str], Any],
    run_slots: Any,
    record_from_state: Callable[..., Any],
    load_from_checkpoints: Callable[..., Any],
    resolve_create_workdir: Callable[..., Any],
    persist_workdir: Callable[..., Any],
    canonical_run_workdir: Callable[..., Any],
    clamp_criteria: Callable[..., Any],
    principal_may_exec: Callable[..., Any],
    require_scope: Callable[..., Any],
    validate_run_id: Callable[..., Any],
    workspace_registry: Any,
    run_service: Any,
    model_config_for_run: Callable[..., Any],
    tenant_has_checkpoints: Callable[..., Any],
    max_criterion_timeout_s: Callable[..., Any],
    load_persisted_workdir: Callable[..., Any],
    settings: Any = None,
) -> AppState:
    pdp = PolicyDecisionPoint.load(surface="api")
    sessions = SessionStore(path=root / "console_sessions.sqlite")
    queue = open_run_queue(root)
    queue.reconcile_orphans()
    bind = getattr(quota_store, "bind_queue", None)
    if bind is not None:
        bind(queue)

    def orchestrator_factory() -> Any:
        import recertia.api as api_mod

        return api_mod.build_default_orchestrator

    def queued_run_record(**kwargs: Any) -> Any:
        from recertia.api.schemas import RunRecord

        return RunRecord(**kwargs)

    def enqueue_async_run(**kwargs: Any) -> None:
        queued = AsyncRunRequest(**kwargs)
        queue.enqueue(
            QueueItem(
                tenant_id=queued.tenant_id,
                run_id=queued.run_id,
                kind="run",
                payload=dump_async_request(queued),
            )
        )

    def resolve_task_class(*, explicit: str | None, goal_task_class: str | None) -> str:
        from recertia.bootstrap import resolve_task_class as resolve

        return resolve(explicit=explicit, goal_task_class=goal_task_class)

    state = AppState(
        root=root,
        skills_root=skills_root,
        facts_root=facts_root,
        key_store=key_store,
        quota_store=quota_store,
        runs=runs,
        run_slots=run_slots,
        record_from_state=record_from_state,
        load_from_checkpoints=load_from_checkpoints,
        resolve_create_workdir=resolve_create_workdir,
        persist_workdir=persist_workdir,
        canonical_run_workdir=canonical_run_workdir,
        clamp_criteria=clamp_criteria,
        principal_may_exec=principal_may_exec,
        require_scope=require_scope,
        validate_run_id=validate_run_id,
        workspace_registry=workspace_registry,
        run_service=run_service,
        pdp=pdp,
        job_service=JobService(pdp=pdp),
        promotion_service=PromotionService(pdp=pdp),
        eval_service=EvalService(pdp=pdp),
        library_service=LibraryService(pdp=pdp),
        program_service=ProgramService(pdp=pdp),
        workspace_service=WorkspaceService(pdp=pdp),
        checkpoint_reader=CheckpointReader(),
        key_service=KeyService(pdp=pdp),
        ops_service=OpsService(pdp=pdp),
        sessions=sessions,
        proposals=ProposalStore(root / "proposals.sqlite"),
        programs=ProgramStore(root / "programs.sqlite"),
        job_runs=JobRunStore(root / "job_runs.sqlite"),
        events=RunEventLog(root),
        queue=queue,
        worker=None,
        embedded=None,
        model_config_for_run=model_config_for_run,
        tenant_has_checkpoints=tenant_has_checkpoints,
        max_criterion_timeout_s=max_criterion_timeout_s,
        load_persisted_workdir=load_persisted_workdir,
        orchestrator_factory=orchestrator_factory,
        queued_run_record=queued_run_record,
        enqueue_async_run=enqueue_async_run,
        resolve_task_class=resolve_task_class,
        settings=settings,
        principal_resolver=IdentityResolver(keys=key_store, sessions=sessions, settings=settings),
    )
    worker = AsyncRunWorker(
        events=state.events,
        on_complete=state._on_async_complete,
        on_failed=state._on_async_failed,
    )
    worker.attach_queue(queue)
    state.worker = worker
    state.embedded = EmbeddedQueueWorker(worker, queue)
    return state
