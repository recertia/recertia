"""HTTP/CLI composition bag. Assembled by ``create_app``; no FastAPI imports."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from recertia.persistence.idempotency import IdempotencyStore

_log = logging.getLogger(__name__)


@dataclass
class AppState:
    """Replaces ``ConsoleContext``. Stored on ``app.state.recertia``."""

    root: Path
    skills_root: Path
    facts_root: Path
    key_store: Any
    quota_store: Any
    runs: dict[tuple[str, str], Any]
    run_slots: Any
    record_from_state: Callable[..., Any]
    load_from_checkpoints: Callable[..., Any]
    resolve_create_workdir: Callable[..., Any]
    persist_workdir: Callable[..., Any]
    canonical_run_workdir: Callable[..., Any]
    clamp_criteria: Callable[..., Any]
    principal_may_exec: Callable[..., Any]
    require_scope: Callable[..., Any]
    validate_run_id: Callable[..., Any]
    workspace_registry: Any
    run_service: Any
    pdp: Any
    job_service: Any
    promotion_service: Any
    eval_service: Any
    library_service: Any
    program_service: Any
    workspace_service: Any
    checkpoint_reader: Any
    key_service: Any
    ops_service: Any
    sessions: Any
    proposals: Any
    programs: Any
    job_runs: Any
    events: Any
    queue: Any
    worker: Any
    embedded: Any
    model_config_for_run: Callable[..., Any]
    tenant_has_checkpoints: Callable[..., Any]
    max_criterion_timeout_s: Callable[..., Any]
    load_persisted_workdir: Callable[..., Any]
    orchestrator_factory: Callable[..., Any]
    queued_run_record: Callable[..., Any]
    enqueue_async_run: Callable[..., Any]
    resolve_task_class: Callable[..., Any]
    settings: Any = None
    principal_resolver: Any = None
    # IdempotencyStore on control.db for program step-run keys (REC-056e). Closed by close().
    idempotency: IdempotencyStore | None = None
    _cancel_flags: set[str] = field(default_factory=set)

    def close(self) -> None:
        """Release what this state owns. Safe to call more than once.

        The embedded queue worker is stopped first: its thread claims from ``queue``
        and runs items that write to these stores, so nothing may be closed under it.
        If it does not stop in time (an in-flight item), every store is left open and
        a warning is logged; a later ``close`` tries again. Then each store's sqlite
        connection is closed. A store that raises does not keep the others open.
        """

        embedded = self.embedded
        if embedded is not None and not embedded.stop():
            _log.warning("app state left open: the embedded queue worker is still running an item")
            return
        failure: BaseException | None = None
        for store in (
            self.idempotency,
            self.queue,
            self.sessions,
            self.proposals,
            self.programs,
            self.job_runs,
            self.workspace_registry,
            self.quota_store,
        ):
            if store is None:
                continue
            try:
                store.close()
            except Exception as exc:  # noqa: BLE001 - close the rest, then raise the first
                failure = failure or exc
        if failure is not None:
            raise failure

    @property
    def jobs(self) -> Any:
        return self.job_service

    @property
    def promotion(self) -> Any:
        return self.promotion_service

    @property
    def evals(self) -> Any:
        return self.eval_service

    @property
    def library(self) -> Any:
        return self.library_service

    @property
    def workspaces(self) -> Any:
        return self.workspace_service

    @property
    def checkpoints(self) -> Any:
        return self.checkpoint_reader

    @property
    def keys(self) -> Any:
        return self.key_service

    @property
    def ops(self) -> Any:
        return self.ops_service

    def tenant_skills_root(self, tenant_id: str) -> Path:
        from recertia.paths import tenant_skills_root

        return tenant_skills_root(
            control_root=self.root, shared=self.skills_root, tenant_id=tenant_id
        )

    def tenant_runs_root(self, tenant_id: str) -> Path:
        return self.root / "runs" / tenant_id

    def _on_async_complete(self, tenant_id: str, run_id: str, state: Any) -> None:
        cost = float(state.spent.cost_usd or 0.0)
        existing = self.runs.get((tenant_id, run_id))
        rec = self.record_from_state(
            run_id=run_id,
            request=state.task.request,
            task_class=state.task.task_class or "repo-chore",
            tenant_id=tenant_id,
            state=state,
            created_at=existing.created_at if existing else datetime.now(timezone.utc),
            has_goal=state.task.goal is not None,
        )
        mode = getattr(existing, "mode", None) or "async"
        extra: dict[str, Any] = {"cost_usd": cost, "mode": mode}
        if existing is not None:
            extra["workspace_id"] = getattr(existing, "workspace_id", None)
            extra["workdir"] = getattr(existing, "workdir", None)
        self.runs[(tenant_id, run_id)] = rec.model_copy(update=extra)
        if self.run_service is not None:
            self.run_service.complete(tenant_id, cost_usd=cost)
        else:
            self.quota_store.complete(tenant_id, cost_usd=cost)

    def _on_async_failed(self, tenant_id: str, run_id: str, *, cancelled: bool = False) -> None:
        if self.run_service is not None:
            self.run_service.release(tenant_id)
        else:
            self.quota_store.release_inflight(tenant_id)
        existing = self.runs.get((tenant_id, run_id))
        if existing is None:
            return
        terminal = "cancelled" if cancelled else "error"
        mode = getattr(existing, "mode", None) or "async"
        self.runs[(tenant_id, run_id)] = existing.model_copy(
            update={"status": terminal, "terminal": terminal, "mode": mode}
        )
