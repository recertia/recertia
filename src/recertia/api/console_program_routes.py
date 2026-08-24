"""Console Goal-pack / migration-program routes."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request

from contracts.goal import Goal, compile_goal
from contracts.program import ExternalHandoff, MigrationProgram, MigrationStep, RepoBinding
from recertia.api.console_deps import RouteState
from recertia.api.console_routes import (
    ProgramAccept,
    ProgramCreate,
    ProgramFromPack,
    RecordTipBody,
    RepoBindingBody,
    SeedWorkdirBody,
    StepPatch,
    StepRunBody,
    StepSkipBody,
)
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


def register_program_routes(app: FastAPI, rs: RouteState) -> None:
    ctx = rs.ctx
    require_runs = rs.require_runs
    _optional_console_user = rs.optional_console_user
    _resolve_tenant = rs.resolve_tenant
    _require_workspace_admin = rs.require_workspace_admin
    _require_library_write = rs.require_library_write
    _public_user = rs.public_user

    @app.post("/v1/programs/from-pack")
    def program_from_pack(
        body: ProgramFromPack,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        """Accept a Compose decomposition/pack draft into a durable MigrationProgram."""

        from contracts.goal import Constraint, DesiredState

        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        steps: list[MigrationStep] = []
        for i, raw in enumerate(body.steps):
            desired_raw = raw.get("desired") or []
            # Ensure each step Goal has ≥1 hard desired
            if not desired_raw:
                desired_raw = [
                    {
                        "id": f"step-{i}-placeholder",
                        "kind": "file_exists",
                        "path": "README.md",
                        "weight": 1.0,
                    }
                ]
            # Strip draft-only fields
            desired = []
            for d in desired_raw:
                clean = {k: v for k, v in d.items() if k in DesiredState.model_fields}
                desired.append(DesiredState.model_validate(clean))
            constraints = []
            for c in raw.get("constraints") or []:
                clean = {k: v for k, v in c.items() if k in Constraint.model_fields}
                constraints.append(Constraint.model_validate(clean))
            goal = Goal(
                desired=desired,
                constraints=constraints,
                context=raw.get("context"),
                task_class=body.task_class,
            )
            role_raw = raw.get("role")
            role: Literal[
                "characterization", "structural", "behaviour_lock", "custom"
            ] = (
                role_raw
                if role_raw
                in {"characterization", "structural", "behaviour_lock", "custom"}
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
            tenant_id=tenant_id,
            title=body.title,
            intent=body.intent,
            task_class=body.task_class,
            decomposition=body.decomposition,
            steps=steps,
            source="heuristic",
            status="draft",
            created_by=str(getattr(principal, "key_id", "") or ""),
        )
        saved = ctx.programs.put(prog)
        return {
            "program": saved.model_dump(mode="json"),
            "warnings": [w.to_dict() for w in stress_program(saved)],
        }

    def _refresh_step_statuses(prog: MigrationProgram) -> MigrationProgram:
        steps = []
        for step in prog.steps:
            updated = step
            if (
                step.current_run_id
                and step.status in {"queued", "running"}
            ):
                rec = ctx.runs.get((prog.tenant_id, step.current_run_id))
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
        # Recompute pack status from step terminals
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
        # Second pass: planned→ready after predecessor may have just succeeded
        steps2 = []
        for step in refreshed.steps:
            if step.status == "planned" and step_is_ready(refreshed, step):
                steps2.append(step.model_copy(update={"status": "ready"}))
            else:
                steps2.append(step)
        return refreshed.model_copy(update={"steps": steps2})

    def _assert_freeze_allowed(enforcement: str) -> None:
        from recertia.programs.materialize import assert_freeze_enforcement_allowed

        try:
            assert_freeze_enforcement_allowed(enforcement)
        except MaterializeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _get_program(program_id: str, tenant_id: str) -> MigrationProgram:
        prog = ctx.programs.get(program_id, tenant_id=tenant_id)
        if prog is None:
            raise HTTPException(status_code=404, detail="program not found")
        return prog

    def _find_step(prog: MigrationProgram, step_id: str) -> MigrationStep:
        for step in prog.steps:
            if step.step_id == step_id:
                return step
        raise HTTPException(status_code=404, detail="step not found")

    def _replace_step(prog: MigrationProgram, updated: MigrationStep) -> MigrationProgram:
        steps = [updated if s.step_id == updated.step_id else s for s in prog.steps]
        return prog.model_copy(update={"steps": steps})

    # ----- GP0 migration programs (Goal packs) -----
    @app.post("/v1/programs")
    def create_program(
        request: Request,
        body: ProgramCreate,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        from contracts.goal import Constraint, DesiredState

        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        _assert_freeze_allowed(body.freeze_enforcement)
        steps = [MigrationStep.model_validate(s) for s in body.steps]
        prog = MigrationProgram(
            program_id=uuid4().hex[:12],
            tenant_id=tenant_id,
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
        saved = ctx.programs.put(prog)
        return {
            "program": saved.model_dump(mode="json"),
            "warnings": [w.to_dict() for w in stress_program(saved)],
        }

    @app.get("/v1/programs")
    def list_programs(
        request: Request,
        status: str | None = None,
        limit: int = Query(50, ge=1, le=100),
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        items = ctx.programs.list(tenant_id=tenant_id, status=status, limit=limit)
        return {"programs": [p.model_dump(mode="json") for p in items]}

    @app.get("/v1/programs/{program_id}")
    def get_program(
        program_id: str,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        prog = _refresh_step_statuses(_get_program(program_id, tenant_id))
        ctx.programs.put(prog)
        return {
            "program": prog.model_dump(mode="json"),
            "warnings": [w.to_dict() for w in stress_program(prog)],
        }

    @app.post("/v1/programs/{program_id}/accept")
    def accept_program(
        program_id: str,
        body: ProgramAccept,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        if not body.ack_disclaimer:
            raise HTTPException(status_code=400, detail="disclaimer must be acknowledged")
        prog = _get_program(program_id, tenant_id)
        _assert_freeze_allowed(prog.freeze_enforcement)
        if prog.status != "draft":
            raise HTTPException(status_code=409, detail="only draft programs can be accepted")
        if not prog.steps:
            raise HTTPException(status_code=400, detail="program has no steps")
        if prog.handoff == "git_tip" and prog.repo_binding is None:
            raise HTTPException(
                status_code=400,
                detail="handoff=git_tip requires a registered repo_binding before accept",
            )
        if prog.handoff == "copy_forward":
            raise HTTPException(
                status_code=400,
                detail="handoff=copy_forward is not supported; use git_tip",
            )
        for step in prog.steps:
            # Goal validation already ensures hard criteria
            if not step.goal.desired:
                raise HTTPException(status_code=400, detail=f"step {step.step_id} missing goal")
        now = datetime.now(timezone.utc).isoformat()
        prog = prog.model_copy(
            update={"status": "active", "disclaimer_acked_at": now}
        )
        prog = _refresh_step_statuses(prog)
        saved = ctx.programs.put(prog)
        return {"program": saved.model_dump(mode="json")}

    @app.post("/v1/programs/{program_id}/abandon")
    def abandon_program(
        program_id: str,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        prog = _get_program(program_id, tenant_id)
        saved = ctx.programs.put(prog.model_copy(update={"status": "abandoned"}))
        return {"program": saved.model_dump(mode="json")}

    @app.patch("/v1/programs/{program_id}/steps/{step_id}")
    def patch_step(
        program_id: str,
        step_id: str,
        body: StepPatch,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        prog = _get_program(program_id, tenant_id)
        step = _find_step(prog, step_id)
        if step.status in {"queued", "running", "succeeded"}:
            raise HTTPException(
                status_code=409,
                detail="step goal is immutable after run bind / success",
            )
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
        prog = _replace_step(prog, updated)
        saved = ctx.programs.put(prog)
        return {"program": saved.model_dump(mode="json")}

    @app.post("/v1/programs/{program_id}/steps/{step_id}/preview")
    def preview_step(
        program_id: str,
        step_id: str,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        prog = _get_program(program_id, tenant_id)
        step = _find_step(prog, step_id)
        try:
            goal = materialize_step_goal(prog, step)
        except MaterializeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        warnings = stress_step(prog, step, goal=goal)
        ph = preview_hash(goal)
        updated = step.model_copy(update={"criteria_preview_hash": ph})
        prog = _replace_step(prog, updated)
        ctx.programs.put(prog)
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

    @app.post("/v1/programs/{program_id}/steps/{step_id}/run")
    def run_step(
        program_id: str,
        step_id: str,
        body: StepRunBody,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        """GP0: plan_only preview envelope, or bind an existing run_id after POST /v1/runs."""

        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        prog = _refresh_step_statuses(_get_program(program_id, tenant_id))
        step = _find_step(prog, step_id)

        if body.idempotency_key:
            idem_key = f"{tenant_id}:{program_id}:{step_id}:{body.idempotency_key}"
            prior = ctx._program_idempotency.get(idem_key)
            if prior and step.current_run_id == prior:
                return {
                    "program": prog.model_dump(mode="json"),
                    "step_id": step_id,
                    "run_id": prior,
                    "idempotent": True,
                }

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
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        warnings = stress_step(prog, step, goal=goal)
        if any(w.severity == "block" for w in warnings):
            raise HTTPException(
                status_code=400,
                detail={"message": "blocked by stress", "warnings": [w.to_dict() for w in warnings]},
            )

        budget = resolve_run_budget(goal, body.budget)
        if prog.budget and prog.budget.max_cost_usd is not None:
            remaining = prog.budget.max_cost_usd - prog.budget.spent_cost_usd
            step_cap = budget.max_cost_usd
            need = float(step_cap) if step_cap is not None else 0.0
            if remaining <= 0 or (step_cap is not None and need > remaining):
                raise HTTPException(status_code=429, detail="program budget exhausted")
        ph = preview_hash(goal)
        # Persist preview hash whenever we materialize for run/envelope
        step = step.model_copy(update={"criteria_preview_hash": ph})
        prog = _replace_step(prog, step)
        ctx.programs.put(prog)

        if body.plan_only or body.bind_run_id is None:
            # Envelope for human confirm → POST /v1/runs → bind
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
            raise HTTPException(status_code=409, detail="program is not active")
        if not step_is_ready(prog, step) and step.status not in {"failed", "ready", "planned"}:
            raise HTTPException(status_code=409, detail="step is not runnable")
        prev = previous_step(prog, step)
        if prev is not None and prev.status not in {"succeeded", "skipped"}:
            raise HTTPException(
                status_code=409,
                detail=f"previous step {prev.step_id} not succeeded",
            )

        run_id = body.bind_run_id
        rec = ctx.runs.get((tenant_id, run_id))
        if rec is None:
            raise HTTPException(status_code=404, detail="run not found for tenant")

        # Bind integrity: preview hash must be current; run criteria_hash must match when set.
        if not step.criteria_preview_hash:
            raise HTTPException(
                status_code=400,
                detail="preview step before bind (missing criteria_preview_hash)",
            )
        if ph != step.criteria_preview_hash:
            raise HTTPException(
                status_code=409,
                detail="goal changed since preview; re-run preview before bind",
            )
        run_hash = rec.criteria_hash
        if run_hash is None:
            loaded = ctx.load_from_checkpoints(ctx.root, tenant_id, run_id)
            if loaded is not None:
                run_hash = loaded.criteria_hash
                if loaded.criteria_hash and (tenant_id, run_id) in ctx.runs:
                    ctx.runs[(tenant_id, run_id)] = rec.model_copy(
                        update={"criteria_hash": loaded.criteria_hash}
                    )
        if run_hash is not None and run_hash != step.criteria_preview_hash:
            raise HTTPException(
                status_code=409,
                detail=(
                    "bound run criteria_hash does not match step criteria_preview_hash; "
                    "submit the materialized Goal from preview"
                ),
            )
        if run_hash is None and (
            rec.terminal in step.acceptance_gate.terminal_in
            or rec.status in step.acceptance_gate.terminal_in
        ):
            # Terminal success without a hash cannot prove lock integrity.
            raise HTTPException(
                status_code=409,
                detail="terminal run missing criteria_hash; cannot verify bind integrity",
            )

        # Idempotent rebinding of same run
        if step.current_run_id == run_id:
            return {
                "program": prog.model_dump(mode="json"),
                "step_id": step_id,
                "run_id": run_id,
                "idempotent": True,
            }
        if step.status in {"queued", "running"} and step.current_run_id:
            raise HTTPException(status_code=409, detail="step already has an in-flight run")

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
                "criteria_preview_hash": ph,
            }
        )
        prog = _replace_step(prog, updated)
        if new_status == "failed":
            prog = prog.model_copy(update={"status": "blocked"})
        elif new_status == "succeeded":
            # complete if all done
            if all(s.status in {"succeeded", "skipped"} for s in prog.steps):
                prog = prog.model_copy(update={"status": "completed"})
            else:
                prog = prog.model_copy(update={"status": "active"})
            prog = _refresh_step_statuses(prog)

        saved = ctx.programs.put(prog)
        if body.idempotency_key:
            ctx._program_idempotency[
                f"{tenant_id}:{program_id}:{step_id}:{body.idempotency_key}"
            ] = run_id
        return {
            "program": saved.model_dump(mode="json"),
            "step_id": step_id,
            "run_id": run_id,
            "step_status": new_status,
            "idempotent": False,
        }

    @app.post("/v1/programs/{program_id}/steps/{step_id}/skip")
    def skip_step(
        program_id: str,
        step_id: str,
        body: StepSkipBody,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        if not (body.note or "").strip():
            raise HTTPException(status_code=400, detail="skip requires a non-empty note")
        prog = _get_program(program_id, tenant_id)
        if prog.status not in {"active", "blocked"}:
            raise HTTPException(status_code=409, detail="program not active")
        step = _find_step(prog, step_id)
        if step.status in {"succeeded", "queued", "running"}:
            raise HTTPException(status_code=409, detail="cannot skip step in current status")
        updated = step.model_copy(
            update={"status": "skipped", "skip_note": body.note.strip()}
        )
        prog = _replace_step(prog, updated)
        if all(s.status in {"succeeded", "skipped"} for s in prog.steps):
            prog = prog.model_copy(update={"status": "completed"})
        else:
            prog = prog.model_copy(update={"status": "active"})
        prog = _refresh_step_statuses(prog)
        saved = ctx.programs.put(prog)
        return {"program": saved.model_dump(mode="json"), "step_id": step_id, "skipped": True}

    @app.post("/v1/programs/{program_id}/repo-binding")
    def set_repo_binding(
        program_id: str,
        body: RepoBindingBody,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        from recertia.programs.git_tip import GitTipError, resolve_binding_root

        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        prog = _get_program(program_id, tenant_id)
        binding = RepoBinding(
            binding_id=body.binding_id,
            root=body.root,
            default_branch=body.default_branch,
            remote_url=body.remote_url,
        )
        try:
            root = resolve_binding_root(ctx.root, tenant_id, binding)
        except GitTipError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        prog = prog.model_copy(update={"repo_binding": binding, "handoff": "git_tip"})
        saved = ctx.programs.put(prog)
        return {
            "program": saved.model_dump(mode="json"),
            "resolved_root": str(root),
        }

    @app.post("/v1/programs/{program_id}/steps/{step_id}/record-tip")
    def record_step_tip(
        program_id: str,
        step_id: str,
        body: RecordTipBody,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        from recertia.programs.git_tip import (
            GitTipError,
            record_tip,
            resolve_binding_root,
        )

        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        prog = _get_program(program_id, tenant_id)
        step = _find_step(prog, step_id)
        if step.status not in {"succeeded", "running", "queued", "ready", "planned"}:
            raise HTTPException(status_code=409, detail="step cannot record tip in this status")
        try:
            if body.use_binding_root:
                if prog.repo_binding is None:
                    raise GitTipError("no repo_binding registered")
                repo = resolve_binding_root(ctx.root, tenant_id, prog.repo_binding)
            else:
                rel = (body.workdir or "").strip().lstrip("/")
                if not rel or ".." in Path(rel).parts:
                    raise GitTipError("workdir required (relative under tenant workspaces)")
                repo = (ctx.root / "workspaces" / tenant_id / rel).resolve()
                try:
                    repo.relative_to((ctx.root / "workspaces" / tenant_id).resolve())
                except ValueError as exc:
                    raise GitTipError("workdir escapes tenant workspaces") from exc
            sha = record_tip(repo)
        except GitTipError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        eh = step.external_handoff or ExternalHandoff()
        eh = eh.model_copy(update={"head_sha": sha})
        updated = step.model_copy(update={"external_handoff": eh})
        prog = _replace_step(prog, updated)
        saved = ctx.programs.put(prog)
        return {"program": saved.model_dump(mode="json"), "head_sha": sha}

    @app.post("/v1/programs/{program_id}/steps/{step_id}/seed-workdir")
    def seed_step_workdir(
        program_id: str,
        step_id: str,
        body: SeedWorkdirBody,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        """Checkout predecessor tip into a fresh canonical run workdir (no shared mount)."""

        from recertia.programs.git_tip import (
            GitTipError,
            checkout_tip,
            resolve_binding_root,
            resolve_tip_sha,
        )

        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        prog = _get_program(program_id, tenant_id)
        step = _find_step(prog, step_id)
        try:
            if prog.handoff != "git_tip":
                raise GitTipError("program handoff is not git_tip")
            if prog.repo_binding is None:
                raise GitTipError("unregistered repo cannot use git_tip")
            tip = resolve_tip_sha(
                prog, step, api_root=ctx.root, explicit=body.tip_sha
            )
            binding_root = resolve_binding_root(ctx.root, tenant_id, prog.repo_binding)
            dest = ctx.canonical_run_workdir(ctx.root, tenant_id, body.run_id)
            checked = checkout_tip(binding_root=binding_root, tip_sha=tip, dest=dest)
        except GitTipError as exc:
            # Mark step failed / program blocked on checkout failure
            failed = step.model_copy(update={"status": "failed"})
            prog = _replace_step(prog, failed).model_copy(update={"status": "blocked"})
            ctx.programs.put(prog)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "run_id": body.run_id,
            "tip_sha": tip,
            "checked_out": checked,
            "workdir": str(dest),
            "program_id": program_id,
            "step_id": step_id,
        }

    # Expose async create helper used by patched POST /v1/runs
    app.state.console_ctx = ctx
