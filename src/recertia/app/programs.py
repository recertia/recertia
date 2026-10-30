"""Migration programs. CLI and HTTP share this service."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import ValidationError

from contracts.goal import Constraint, DesiredState, Goal, compile_goal
from contracts.program import ExternalHandoff, MigrationProgram, MigrationStep, RepoBinding
from recertia.app.authz import Resource
from recertia.errors import ConfigError, Conflict, ExecutionUnavailable, NotFound, QuotaExceeded
from recertia.persistence.idempotency import Completion, IdempotencyStore, KeyScope, Outcome, Reservation
from recertia.ports.identity import Principal
from recertia.programs.materialize import (
    MaterializeError,
    assert_gp0_execution_prereqs,
    materialize_step_goal,
    preview_hash,
    previous_step,
    resolve_run_budget,
    step_is_ready,
)
from recertia.programs.stress import stress_program, stress_step
from recertia.runtime.errors import (
    IdempotencyInProgress,
    IdempotencyMismatch,
    PreconditionFailed,
    PreconditionRequired,
    StateCorrupt,
)


def step_run_request_hash(body: Any) -> str:
    """``v1:`` + sha256 of the step-run body without its key, as canonical JSON.

    Every field is dumped with its default filled in, so an omitted field and the same
    field sent with its default value hash alike.
    """

    fields = body.model_dump(mode="json", exclude={"idempotency_key"})
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "v1:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _idempotency_store(runtime: Any) -> IdempotencyStore:
    """The runtime's idempotency store, needed for a keyed bind."""

    store: IdempotencyStore | None = runtime.idempotency
    if store is None:
        # create_app opens the store (api/composition.py). A runtime without one cannot
        # honour the key, so refuse before any write instead of binding unguarded.
        raise ExecutionUnavailable("idempotency_key needs the idempotency store, which is not open")
    return store


def _held_lease(reservation: Reservation, program_id: str, step_id: str) -> int:
    """The lease of a RESERVED or TAKEN_OVER reservation, which always carries one."""

    if reservation.lease is None:
        # Without the lease the row can be neither completed nor released, so nothing is bound.
        raise StateCorrupt(
            "idempotency reservation carries no lease",
            details={"program_id": program_id, "step_id": step_id},
        )
    return reservation.lease


class ProgramService:
    """``pdp.require`` then ProgramStore. Routes do not mutate programs."""

    def __init__(self, *, pdp: Any | None = None) -> None:
        self._pdp = pdp

    def create_from_pack(
        self, principal: Principal, *, tenant: str, runtime: Any, body: Any
    ) -> dict[str, Any]:
        self._require(principal, "programs.write")
        steps: list[MigrationStep] = []
        for i, raw in enumerate(body.steps):
            desired_raw = raw.get("desired") or []
            if not desired_raw:
                desired_raw = [
                    {
                        "id": f"step-{i}-placeholder",
                        "kind": "file_exists",
                        "path": "README.md",
                        "weight": 1.0,
                    }
                ]
            desired = []
            for item in desired_raw:
                clean = {k: v for k, v in item.items() if k in DesiredState.model_fields}
                desired.append(DesiredState.model_validate(clean))
            constraints = []
            for item in raw.get("constraints") or []:
                clean = {k: v for k, v in item.items() if k in Constraint.model_fields}
                constraints.append(Constraint.model_validate(clean))
            goal = Goal(
                desired=desired,
                constraints=constraints,
                context=raw.get("context"),
                task_class=body.task_class,
            )
            role_raw = raw.get("role")
            role: Literal["characterization", "structural", "behaviour_lock", "custom"] = (
                role_raw
                if role_raw in {"characterization", "structural", "behaviour_lock", "custom"}
                else "custom"
            )
            steps.append(
                MigrationStep(
                    step_id=raw.get("step_id") or f"s{i}",
                    ordinal=int(raw.get("ordinal", i)),
                    title=raw.get("title") or f"Step {i}",
                    role=role,
                    goal=goal,
                    freeze_paths=list(raw.get("freeze_paths") or []),
                    mutate_paths=list(raw.get("mutate_paths") or []),
                )
            )
        prog = MigrationProgram(
            program_id=uuid4().hex[:12],
            tenant_id=tenant,
            title=body.title,
            intent=body.intent,
            task_class=body.task_class,
            decomposition=body.decomposition,
            steps=steps,
            source="heuristic",
            status="draft",
            created_by=str(getattr(principal, "key_id", "") or ""),
        )
        saved = runtime.programs.put(prog)
        return {
            "program": saved.model_dump(mode="json"),
            "warnings": [w.to_dict() for w in stress_program(saved)],
        }

    def create(
        self, principal: Principal, *, tenant: str, runtime: Any, body: Any
    ) -> dict[str, Any]:
        self._require(principal, "programs.write")
        self._assert_freeze_allowed(body.freeze_enforcement)
        steps = [MigrationStep.model_validate(s) for s in body.steps]
        prog = MigrationProgram(
            program_id=uuid4().hex[:12],
            tenant_id=tenant,
            title=body.title,
            intent=body.intent,
            task_class=body.task_class,
            decomposition=body.decomposition,
            handoff=body.handoff,
            freeze_enforcement=body.freeze_enforcement,
            steps=steps,
            program_bar_desired=[DesiredState.model_validate(d) for d in body.program_bar_desired],
            program_bar_constraints=[
                Constraint.model_validate(c) for c in body.program_bar_constraints
            ],
            source=body.source,
            created_by=str(getattr(principal, "key_id", "") or ""),
            status="draft",
        )
        saved = runtime.programs.put(prog)
        return {
            "program": saved.model_dump(mode="json"),
            "warnings": [w.to_dict() for w in stress_program(saved)],
        }

    def list(
        self,
        principal: Principal,
        *,
        tenant: str,
        runtime: Any,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        self._require(principal, "programs.read")
        items = runtime.programs.list(tenant_id=tenant, status=status, limit=limit)
        return {"programs": [p.model_dump(mode="json") for p in items]}

    def get(
        self, principal: Principal, *, tenant: str, runtime: Any, program_id: str
    ) -> dict[str, Any]:
        self._require(principal, "programs.read")
        current = self._get_program(runtime, program_id, tenant)
        prog = self._refresh_step_statuses(current, runtime)
        # Save only a real change: every save bumps the version, so a read that saved
        # would make the version a client just read stale. Pin the save to the version
        # read, so a write that landed since is never overwritten by this refresh.
        if prog != current:
            try:
                prog = runtime.programs.put(prog, expected_version=current.version)
            except PreconditionFailed:
                prog = self._get_program(runtime, program_id, tenant)
        return {
            "program": prog.model_dump(mode="json"),
            "warnings": [w.to_dict() for w in stress_program(prog)],
        }

    def replace(
        self,
        principal: Principal,
        *,
        tenant: str,
        runtime: Any,
        program_id: str,
        body: Any,
        if_match: int | None,
    ) -> dict[str, Any]:
        """Replace a draft program's editable fields if ``if_match`` is the stored version.

        Server-owned fields (status, owner, timestamps, repo binding, budget) are taken
        from the program read here, so ``if_match`` must equal that read's version, and the
        store write is pinned to the same version: a save that lands between this read and
        the write gets ``PreconditionFailed`` instead of being overwritten.
        """

        self._require(principal, "programs.write")
        current = self._get_program(runtime, program_id, tenant)
        if if_match is None:
            raise PreconditionRequired("If-Match is required to replace an existing program")
        if if_match != current.version:
            raise PreconditionFailed(
                f"program version {if_match} is not the stored version",
                details={"stored_version": current.version},
            )
        if current.status != "draft":
            raise Conflict("only draft programs can be replaced")
        self._assert_freeze_allowed(body.freeze_enforcement)
        data = current.model_dump(mode="json")
        data.update(
            title=body.title,
            intent=body.intent,
            task_class=body.task_class,
            decomposition=body.decomposition,
            handoff=body.handoff,
            freeze_enforcement=body.freeze_enforcement,
            steps=body.steps,
            program_bar_desired=body.program_bar_desired,
            program_bar_constraints=body.program_bar_constraints,
            source=body.source,
        )
        try:
            prog = MigrationProgram.model_validate(data)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first["loc"]) or "program"
            raise ConfigError(f"invalid program: {where}: {first['msg']}") from exc
        saved = runtime.programs.put(prog, expected_version=current.version)
        return {
            "program": saved.model_dump(mode="json"),
            "warnings": [w.to_dict() for w in stress_program(saved)],
        }

    def accept(
        self, principal: Principal, *, tenant: str, runtime: Any, program_id: str, body: Any
    ) -> dict[str, Any]:
        self._require(principal, "programs.write")
        if not body.ack_disclaimer:
            raise ConfigError("disclaimer must be acknowledged")
        prog = self._get_program(runtime, program_id, tenant)
        self._assert_freeze_allowed(prog.freeze_enforcement)
        if prog.status != "draft":
            raise Conflict("only draft programs can be accepted")
        if not prog.steps:
            raise ConfigError("program has no steps")
        if prog.handoff == "git_tip" and prog.repo_binding is None:
            raise ConfigError("handoff=git_tip requires a registered repo_binding before accept")
        if prog.handoff == "copy_forward":
            raise ConfigError("handoff=copy_forward is not supported; use git_tip")
        for step in prog.steps:
            if not step.goal.desired:
                raise ConfigError(f"step {step.step_id} missing goal")
        now = datetime.now(timezone.utc).isoformat()
        prog = prog.model_copy(update={"status": "active", "disclaimer_acked_at": now})
        prog = self._refresh_step_statuses(prog, runtime)
        saved = runtime.programs.put(prog)
        return {"program": saved.model_dump(mode="json")}

    def abandon(
        self, principal: Principal, *, tenant: str, runtime: Any, program_id: str
    ) -> dict[str, Any]:
        self._require(principal, "programs.write")
        prog = self._get_program(runtime, program_id, tenant)
        saved = runtime.programs.put(prog.model_copy(update={"status": "abandoned"}))
        return {"program": saved.model_dump(mode="json")}

    def patch_step(
        self,
        principal: Principal,
        *,
        tenant: str,
        runtime: Any,
        program_id: str,
        step_id: str,
        body: Any,
    ) -> dict[str, Any]:
        self._require(principal, "programs.write")
        prog = self._get_program(runtime, program_id, tenant)
        step = self._find_step(prog, step_id)
        if step.status in {"queued", "running", "succeeded"}:
            raise Conflict("step goal is immutable after run bind / success")
        updates: dict[str, Any] = {}
        if body.title is not None:
            updates["title"] = body.title
        if body.goal is not None:
            updates["goal"] = body.goal
            updates["goal_revision"] = step.goal_revision + 1
            updates["criteria_preview_hash"] = None
        if body.freeze_paths is not None:
            updates["freeze_paths"] = body.freeze_paths
        if body.mutate_paths is not None:
            updates["mutate_paths"] = body.mutate_paths
        if body.role is not None:
            updates["role"] = body.role
        if body.external_handoff is not None:
            updates["external_handoff"] = body.external_handoff
        updated = step.model_copy(update=updates)
        prog = self._replace_step(prog, updated)
        saved = runtime.programs.put(prog)
        return {"program": saved.model_dump(mode="json")}

    def preview_step(
        self,
        principal: Principal,
        *,
        tenant: str,
        runtime: Any,
        program_id: str,
        step_id: str,
    ) -> dict[str, Any]:
        self._require(principal, "programs.read")
        prog = self._get_program(runtime, program_id, tenant)
        step = self._find_step(prog, step_id)
        try:
            goal = materialize_step_goal(prog, step)
        except MaterializeError as exc:
            raise ConfigError(str(exc)) from exc
        warnings = stress_step(prog, step, goal=goal)
        ph = preview_hash(goal)
        if step.criteria_preview_hash != ph:
            # Save only a new hash: a read-scoped preview must not bump the version.
            updated = step.model_copy(update={"criteria_preview_hash": ph})
            prog = self._replace_step(prog, updated)
            runtime.programs.put(prog)
        criteria = [c.model_dump(mode="json") for c in compile_goal(goal)]
        blocked = any(w.severity == "block" for w in warnings)
        return {
            "goal": goal.model_dump(mode="json"),
            "criteria": criteria,
            "criteria_preview_hash": ph,
            "budget": resolve_run_budget(goal).model_dump(mode="json"),
            "warnings": [w.to_dict() for w in warnings],
            "blocked": blocked,
            "freeze_enforcement": prog.freeze_enforcement,
        }

    def run_step(
        self,
        principal: Principal,
        *,
        tenant: str,
        runtime: Any,
        program_id: str,
        step_id: str,
        body: Any,
    ) -> dict[str, Any]:
        self._require(principal, "programs.write")
        prog, step = self._load_step(runtime, program_id, tenant, step_id)
        # The key only guards a bind: plan_only and an unbound call write no step state.
        if body.idempotency_key is None or body.plan_only or body.bind_run_id is None:
            return self._apply_run_step(
                tenant=tenant,
                runtime=runtime,
                program_id=program_id,
                step_id=step_id,
                body=body,
                prog=prog,
                step=step,
            )

        store = _idempotency_store(runtime)
        scope = KeyScope(tenant, program_id, step_id, body.idempotency_key)
        request_hash = step_run_request_hash(body)
        # Write order (REC-056e): 1. a pending row in control.db; 2. the bind in
        # programs.sqlite; 3. the row marked complete. See persistence/idempotency.py.
        reservation = store.reserve(scope, request_hash)
        if reservation.outcome is Outcome.MISMATCH:
            raise IdempotencyMismatch("idempotency_key was already used with a different request body")
        if reservation.outcome is Outcome.IN_PROGRESS:
            raise IdempotencyInProgress(
                "a request with this idempotency_key is still in progress", retry_after_s=1
            )
        if reservation.outcome is Outcome.REPLAY:
            # Read again: the program read above may predate the bind this key recorded.
            prog, step = self._load_step(runtime, program_id, tenant, step_id)
            if step.current_run_id != reservation.run_id:
                raise Conflict("idempotency key already applied; step has moved on")
            return {
                "program": prog.model_dump(mode="json"),
                "step_id": step_id,
                "run_id": reservation.run_id,
                "idempotent": True,
            }
        lease = _held_lease(reservation, program_id, step_id)
        try:
            # Read again under the reservation, so the bind sees any write that landed first.
            prog, step = self._load_step(runtime, program_id, tenant, step_id)
            if reservation.outcome is Outcome.TAKEN_OVER and step.current_run_id == body.bind_run_id:
                # The request that died held this key and its bind landed: finish its row.
                result = {
                    "program": prog.model_dump(mode="json"),
                    "step_id": step_id,
                    "run_id": body.bind_run_id,
                    "idempotent": True,
                }
            else:
                result = self._apply_run_step(
                    tenant=tenant,
                    runtime=runtime,
                    program_id=program_id,
                    step_id=step_id,
                    body=body,
                    prog=prog,
                    step=step,
                )
        except BaseException:
            store.release(scope, request_hash, lease=lease)
            raise
        completion = store.complete(scope, request_hash, str(result["run_id"]), lease=lease)
        if completion is Completion.SUPERSEDED:
            # This request was slow, not dead: a retry took its row over and now owns it.
            raise IdempotencyInProgress(
                "a retry with this idempotency_key took over the request and is still in progress",
                retry_after_s=1,
            )
        if completion is not Completion.COMPLETED:
            raise StateCorrupt(
                "idempotency key row was not pending when its bind completed",
                details={"program_id": program_id, "step_id": step_id},
            )
        return result

    def _load_step(
        self, runtime: Any, program_id: str, tenant: str, step_id: str
    ) -> tuple[MigrationProgram, MigrationStep]:
        prog = self._refresh_step_statuses(self._get_program(runtime, program_id, tenant), runtime)
        return prog, self._find_step(prog, step_id)

    def _apply_run_step(
        self,
        *,
        tenant: str,
        runtime: Any,
        program_id: str,
        step_id: str,
        body: Any,
        prog: MigrationProgram,
        step: MigrationStep,
    ) -> dict[str, Any]:
        try:
            goal = materialize_step_goal(prog, step)
            assert_gp0_execution_prereqs(
                prog,
                step,
                workdir=body.workdir,
                workspace_id=body.workspace_id,
                plan_only=body.plan_only,
            )
        except MaterializeError as exc:
            raise ConfigError(str(exc)) from exc

        warnings = stress_step(prog, step, goal=goal)
        if any(w.severity == "block" for w in warnings):
            raise ConfigError(
                "blocked by stress",
                details={
                    "http_detail": {
                        "message": "blocked by stress",
                        "warnings": [w.to_dict() for w in warnings],
                    }
                },
            )

        budget = resolve_run_budget(goal, body.budget)
        if prog.budget and prog.budget.max_cost_usd is not None:
            remaining = prog.budget.max_cost_usd - prog.budget.spent_cost_usd
            step_cap = budget.max_cost_usd
            need = float(step_cap) if step_cap is not None else 0.0
            if remaining <= 0 or (step_cap is not None and need > remaining):
                raise QuotaExceeded("program budget exhausted")
        ph = preview_hash(goal)
        # The hash the last preview stored. The bind checks below compare against it, so
        # read it before this call's hash replaces it, and store nothing for a bind yet: a
        # rejected bind must not count as a preview.
        previewed_hash = step.criteria_preview_hash
        step = step.model_copy(update={"criteria_preview_hash": ph})
        prog = self._replace_step(prog, step)

        if body.plan_only or body.bind_run_id is None:
            runtime.programs.put(prog)
            return {
                "plan_only": body.plan_only or body.bind_run_id is None,
                "run_create": {
                    "goal": goal.model_dump(mode="json"),
                    "task_class": goal.task_class or prog.task_class,
                    "budget": budget.model_dump(mode="json"),
                    "workdir": body.workdir,
                    "workspace_id": body.workspace_id,
                },
                "criteria_preview_hash": ph,
                "warnings": [w.to_dict() for w in warnings],
                "blocked": False,
                "ready": step_is_ready(prog, step),
                "hint": "POST /v1/runs with run_create, then POST this endpoint with bind_run_id",
            }

        if prog.status != "active":
            raise Conflict("program is not active")
        if not step_is_ready(prog, step) and step.status not in {"failed", "ready", "planned"}:
            raise Conflict("step is not runnable")
        prev = previous_step(prog, step)
        if prev is not None and prev.status not in {"succeeded", "skipped"}:
            raise Conflict(f"previous step {prev.step_id} not succeeded")

        run_id = body.bind_run_id
        rec = runtime.runs.get((tenant, run_id))
        if rec is None:
            raise NotFound("run not found for tenant")

        if not previewed_hash:
            raise ConfigError("preview step before bind (missing criteria_preview_hash)")
        if ph != previewed_hash:
            raise Conflict("goal changed since preview; re-run preview before bind")
        run_hash = rec.criteria_hash
        if run_hash is None:
            loaded = runtime.load_from_checkpoints(runtime.root, tenant, run_id)
            if loaded is not None:
                run_hash = loaded.criteria_hash
                if loaded.criteria_hash and (tenant, run_id) in runtime.runs:
                    runtime.runs[(tenant, run_id)] = rec.model_copy(
                        update={"criteria_hash": loaded.criteria_hash}
                    )
        if run_hash is not None and run_hash != ph:
            raise Conflict(
                "bound run criteria_hash does not match step criteria_preview_hash; "
                "submit the materialized Goal from preview"
            )
        if run_hash is None and (
            rec.terminal in step.acceptance_gate.terminal_in
            or rec.status in step.acceptance_gate.terminal_in
        ):
            raise Conflict("terminal run missing criteria_hash; cannot verify bind integrity")

        if step.current_run_id == run_id:
            return {
                "program": prog.model_dump(mode="json"),
                "step_id": step_id,
                "run_id": run_id,
                "idempotent": True,
            }
        if step.status in {"queued", "running"} and step.current_run_id:
            raise Conflict("step already has an in-flight run")

        run_ids = list(step.run_ids)
        if run_id not in run_ids:
            run_ids.append(run_id)

        terminal = rec.terminal or rec.status
        gate = step.acceptance_gate.terminal_in
        if terminal in gate:
            new_status = "succeeded"
        elif terminal in {"queued", "running", None} or rec.status in {"queued", "running"}:
            new_status = "running" if rec.status == "running" else "queued"
        else:
            new_status = "failed"

        updated = step.model_copy(
            update={
                "run_ids": run_ids,
                "current_run_id": run_id,
                "status": new_status,
            }
        )
        prog = self._replace_step(prog, updated)
        if new_status == "failed":
            prog = prog.model_copy(update={"status": "blocked"})
        elif new_status == "succeeded":
            if all(s.status in {"succeeded", "skipped"} for s in prog.steps):
                prog = prog.model_copy(update={"status": "completed"})
            else:
                prog = prog.model_copy(update={"status": "active"})
            prog = self._refresh_step_statuses(prog, runtime)

        saved = runtime.programs.put(prog)
        return {
            "program": saved.model_dump(mode="json"),
            "step_id": step_id,
            "run_id": run_id,
            "step_status": new_status,
            "idempotent": False,
        }

    def skip_step(
        self,
        principal: Principal,
        *,
        tenant: str,
        runtime: Any,
        program_id: str,
        step_id: str,
        body: Any,
    ) -> dict[str, Any]:
        self._require(principal, "programs.write")
        if not (body.note or "").strip():
            raise ConfigError("skip requires a non-empty note")
        prog = self._get_program(runtime, program_id, tenant)
        if prog.status not in {"active", "blocked"}:
            raise Conflict("program not active")
        step = self._find_step(prog, step_id)
        if step.status in {"succeeded", "queued", "running"}:
            raise Conflict("cannot skip step in current status")
        updated = step.model_copy(update={"status": "skipped", "skip_note": body.note.strip()})
        prog = self._replace_step(prog, updated)
        if all(s.status in {"succeeded", "skipped"} for s in prog.steps):
            prog = prog.model_copy(update={"status": "completed"})
        else:
            prog = prog.model_copy(update={"status": "active"})
        prog = self._refresh_step_statuses(prog, runtime)
        saved = runtime.programs.put(prog)
        return {"program": saved.model_dump(mode="json"), "step_id": step_id, "skipped": True}

    def bind_repo(
        self, principal: Principal, *, tenant: str, runtime: Any, program_id: str, body: Any
    ) -> dict[str, Any]:
        self._require(principal, "programs.write")
        from recertia.programs.git_tip import GitTipError, resolve_binding_root

        prog = self._get_program(runtime, program_id, tenant)
        binding = RepoBinding(
            binding_id=body.binding_id,
            root=body.root,
            default_branch=body.default_branch,
            remote_url=body.remote_url,
        )
        try:
            root = resolve_binding_root(runtime.root, tenant, binding)
        except GitTipError as exc:
            raise ConfigError(str(exc)) from exc
        prog = prog.model_copy(update={"repo_binding": binding, "handoff": "git_tip"})
        saved = runtime.programs.put(prog)
        return {"program": saved.model_dump(mode="json"), "resolved_root": str(root)}

    def record_tip(
        self,
        principal: Principal,
        *,
        tenant: str,
        runtime: Any,
        program_id: str,
        step_id: str,
        body: Any,
    ) -> dict[str, Any]:
        self._require(principal, "programs.write")
        from recertia.programs.git_tip import GitTipError, record_tip, resolve_binding_root

        prog = self._get_program(runtime, program_id, tenant)
        step = self._find_step(prog, step_id)
        if step.status not in {"succeeded", "running", "queued", "ready", "planned"}:
            raise Conflict("step cannot record tip in this status")
        try:
            if body.use_binding_root:
                if prog.repo_binding is None:
                    raise GitTipError("no repo_binding registered")
                repo = resolve_binding_root(runtime.root, tenant, prog.repo_binding)
            else:
                rel = (body.workdir or "").strip().lstrip("/")
                if not rel or ".." in Path(rel).parts:
                    raise GitTipError("workdir required (relative under tenant workspaces)")
                repo = (runtime.root / "workspaces" / tenant / rel).resolve()
                try:
                    repo.relative_to((runtime.root / "workspaces" / tenant).resolve())
                except ValueError as exc:
                    raise GitTipError("workdir escapes tenant workspaces") from exc
            sha = record_tip(repo)
        except GitTipError as exc:
            raise ConfigError(str(exc)) from exc
        eh = step.external_handoff or ExternalHandoff()
        eh = eh.model_copy(update={"head_sha": sha})
        updated = step.model_copy(update={"external_handoff": eh})
        prog = self._replace_step(prog, updated)
        saved = runtime.programs.put(prog)
        return {"program": saved.model_dump(mode="json"), "head_sha": sha}

    def seed_workdir(
        self,
        principal: Principal,
        *,
        tenant: str,
        runtime: Any,
        program_id: str,
        step_id: str,
        body: Any,
    ) -> dict[str, Any]:
        self._require(principal, "programs.write")
        from recertia.programs.git_tip import (
            GitTipError,
            checkout_tip,
            resolve_binding_root,
            resolve_tip_sha,
        )

        prog = self._get_program(runtime, program_id, tenant)
        step = self._find_step(prog, step_id)
        try:
            if prog.handoff != "git_tip":
                raise GitTipError("program handoff is not git_tip")
            if prog.repo_binding is None:
                raise GitTipError("unregistered repo cannot use git_tip")
            tip = resolve_tip_sha(prog, step, api_root=runtime.root, explicit=body.tip_sha)
            binding_root = resolve_binding_root(runtime.root, tenant, prog.repo_binding)
            dest = runtime.canonical_run_workdir(runtime.root, tenant, body.run_id)
            checked = checkout_tip(binding_root=binding_root, tip_sha=tip, dest=dest)
        except GitTipError as exc:
            failed = step.model_copy(update={"status": "failed"})
            prog = self._replace_step(prog, failed).model_copy(update={"status": "blocked"})
            runtime.programs.put(prog)
            raise ConfigError(str(exc)) from exc
        return {
            "run_id": body.run_id,
            "tip_sha": tip,
            "checked_out": checked,
            "workdir": str(dest),
            "program_id": program_id,
            "step_id": step_id,
        }

    def _require(self, principal: Principal, action: str) -> None:
        pdp = self._pdp
        if pdp is None:
            return
        pdp.require(principal, action, Resource(tenant_id=principal.tenant_id))

    def _assert_freeze_allowed(self, enforcement: str) -> None:
        from recertia.programs.materialize import assert_freeze_enforcement_allowed

        try:
            assert_freeze_enforcement_allowed(enforcement)
        except MaterializeError as exc:
            raise ConfigError(str(exc)) from exc

    def _get_program(self, runtime: Any, program_id: str, tenant_id: str) -> MigrationProgram:
        prog = runtime.programs.get(program_id, tenant_id=tenant_id)
        if prog is None:
            raise NotFound("program not found")
        return prog

    def _find_step(self, prog: MigrationProgram, step_id: str) -> MigrationStep:
        for step in prog.steps:
            if step.step_id == step_id:
                return step
        raise NotFound("step not found")

    def _replace_step(self, prog: MigrationProgram, updated: MigrationStep) -> MigrationProgram:
        steps = [updated if s.step_id == updated.step_id else s for s in prog.steps]
        return prog.model_copy(update={"steps": steps})

    def _refresh_step_statuses(self, prog: MigrationProgram, runtime: Any) -> MigrationProgram:
        steps = []
        for step in prog.steps:
            updated = step
            if step.current_run_id and step.status in {"queued", "running"}:
                rec = runtime.runs.get((prog.tenant_id, step.current_run_id))
                if rec is not None:
                    terminal = rec.terminal or rec.status
                    gate = step.acceptance_gate.terminal_in
                    if terminal in gate:
                        new_status = "succeeded"
                    elif terminal in {"queued"} or rec.status == "queued":
                        new_status = "queued"
                    elif terminal in {"running"} or rec.status == "running":
                        new_status = "running"
                    elif terminal:
                        new_status = "failed"
                    else:
                        new_status = step.status
                    if new_status != step.status:
                        updated = step.model_copy(update={"status": new_status})
            if updated.status == "planned" and step_is_ready(prog, updated):
                updated = updated.model_copy(update={"status": "ready"})
            steps.append(updated)
        refreshed = prog.model_copy(update={"steps": steps})
        if refreshed.status in {"active", "blocked"}:
            if any(s.status == "failed" for s in refreshed.steps):
                refreshed = refreshed.model_copy(update={"status": "blocked"})
            elif refreshed.steps and all(
                s.status in {"succeeded", "skipped"} for s in refreshed.steps
            ):
                refreshed = refreshed.model_copy(update={"status": "completed"})
            elif refreshed.status == "blocked" and not any(
                s.status == "failed" for s in refreshed.steps
            ):
                refreshed = refreshed.model_copy(update={"status": "active"})
        steps2 = []
        for step in refreshed.steps:
            if step.status == "planned" and step_is_ready(refreshed, step):
                steps2.append(step.model_copy(update={"status": "ready"}))
            else:
                steps2.append(step)
        return refreshed.model_copy(update={"steps": steps2})
