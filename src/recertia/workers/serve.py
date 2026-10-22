"""Process entry for ``recertia worker``. One claim at a time, in this process."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from contracts.budget import Budget
from contracts.common import Arm
from contracts.criteria import TaskCriterion
from contracts.run import Task
from recertia.graph.hooks import CancelHopHook, RunCancelled
from recertia.persistence.root_lock import RootBusy
from recertia.ports.identity import Principal
from recertia.workers.queue import ROOT_BUSY_RETRY_S, Claim, RunQueue, open_run_queue


def _arm(value: object) -> Arm:
    if value == "control":
        return "control"
    if value == "shadow":
        return "shadow"
    if value == "practice":
        return "practice"
    if value == "treatment":
        return "treatment"
    raise RuntimeError(f"unsupported arm {value!r}")


def _script(value: object) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return list(value)
    raise RuntimeError("script must be a list of strings")


def _hostname() -> str:
    uname = getattr(os, "uname", None)
    if uname is None:
        return "localhost"
    return str(uname().nodename)


def worker_id() -> str:
    return f"{_hostname()}-{os.getpid()}"


def execute_claim(root: Path, claim: Claim, *, queue: RunQueue | None = None) -> None:
    """Run one queued item to a graph return. The queue row is updated by the caller."""

    if claim.kind != "run":
        raise RuntimeError(f"unsupported queue kind {claim.kind!r}")
    payload = claim.payload
    raw_criteria = payload.get("criteria") or [
        {
            "id": "output-exists",
            "kind": "command",
            "run": "test -f output.txt",
            "source": "caller",
            "weight": 1.0,
        }
    ]
    criteria = [TaskCriterion.model_validate(item) for item in raw_criteria]
    budget_raw = payload.get("budget")
    budget = Budget.model_validate(budget_raw) if isinstance(budget_raw, dict) else Budget()
    default_workdir = root / "workspaces" / claim.tenant_id / claim.run_id
    workdir = Path(str(payload.get("workdir") or default_workdir))
    workdir.mkdir(parents=True, exist_ok=True)
    chosen_arm = _arm(payload.get("arm") or "treatment")
    script = _script(payload.get("script"))
    max_steps = payload.get("max_steps")
    step_cap = max_steps if isinstance(max_steps, int) else None
    request = payload.get("request")
    if not isinstance(request, str) or not request:
        request = claim.run_id
    task_class = payload.get("task_class")
    if not isinstance(task_class, str):
        task_class = None
    runs_root = root / "runs" / claim.tenant_id
    approve = bool(payload.get("approve_tools"))
    from recertia.bootstrap import build_default_orchestrator
    from recertia.paths import tenant_skills_root

    scopes = frozenset({"runs", "exec"}) if approve else frozenset({"runs"})
    shared = Path(str(payload.get("skills_root") or (root / "skills")))
    skills = tenant_skills_root(control_root=root, shared=shared, tenant_id=claim.tenant_id)
    principal = Principal.from_api_key(
        key_id="recertia-worker", tenant_id=claim.tenant_id, scopes=scopes
    )
    bundle = build_default_orchestrator(
        runs_root,
        skills_root=skills,
        facts_root=Path(str(payload.get("facts_root") or (root / "facts"))),
        index_path=runs_root / "skill_index.db",
        approve_default_tools=approve,
        principal=principal,
        surface="worker",
        runtime_root=root,
    )
    try:
        if queue is not None:
            bundle.orchestrator.hooks = [
                CancelHopHook(lambda: queue.cancel_requested(claim.tenant_id, claim.run_id)),
                *bundle.orchestrator.hooks,
            ]
        action = str(payload.get("action") or "start")
        if action == "resume":
            bundle.orchestrator.resume(claim.run_id, workdir=workdir, script=script)
        else:
            bundle.orchestrator.start(
                claim.run_id,
                Task(
                    task_id=claim.run_id,
                    request=request,
                    task_class=task_class,
                    submitted_at=datetime.now(timezone.utc),
                    submitted_by="recertia-worker",
                ),
                criteria,
                budget=budget,
                workdir=workdir,
                script=script,
                arm=chosen_arm,
                max_steps=step_cap,
                manifest=bundle.run_manifest(),
            )
    finally:
        bundle.close()


def run_once(root: Path, *, worker: str, capacity: int = 1) -> int:
    """Claim and execute a single due item. Returns 1 when an item ran, else 0.

    A claim refused with ``RootBusy`` is retryable: it goes back to ``queued``
    after ``ROOT_BUSY_RETRY_S`` without spending an attempt, and this returns 0.
    """

    queue = open_run_queue(root)
    claim: Claim | None = None
    try:
        queue.reconcile_orphans()
        queue.register_worker(
            worker, capacity=capacity, hostname=_hostname(), pid=os.getpid()
        )
        claims = queue.claim(worker, limit=1)
        if not claims:
            return 0
        claim = claims[0]
        queue.heartbeat_worker(worker)
        queue.heartbeat_claim(claim)
        queue.mark_running(claim)
        try:
            execute_claim(root, claim, queue=queue)
        except RunCancelled:
            queue.finish(claim, status="cancelled")
            return 1
        except RootBusy:
            # Backup, restore, an import or another tenant's open handle holds the
            # runtime root lock. Nothing ran; the run is not at fault (REC-058 MF-C).
            queue.requeue(claim, delay_s=ROOT_BUSY_RETRY_S, error_code="root_busy")
            return 0
        except Exception:
            queue.finish(claim, status="failed", error_code="run_failed")
            raise
        queue.finish(claim, status="succeeded")
        return 1
    finally:
        queue.close()
