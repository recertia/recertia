"""Console library routes: goals, templates, runs, skills, metrics, proposals, jobs, tower."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from recertia.api.console_deps import RouteState
from recertia.api.console_routes import (
    GoalPreview,
    GoalProbe,
    GoalSuggest,
    JobTrigger,
    ProposalDecision,
)
from recertia.app.jobs import JobDispatch
from recertia.errors import ConfigError, Forbidden, NotFound
from recertia.evals.store import EvalStore
from recertia.jobs import JobBudget, build_job_runner
from recertia.jobs.enablement import attach_enablement
from recertia.ledger import HashChainLedger
from recertia.memory.procedural.promote import PromotionError
from recertia.memory.procedural.store import SkillStore
from recertia.ports.identity import ledger_actor
from recertia.proposals.store import ProposalRecord
from recertia.solver.transcript import TranscriptStore
from recertia.trajectory.store import TrajectoryStore


def _tenant_owns_run(ctx: Any, tenant_id: str, run_id: str) -> bool:
    """True when this tenant has the run in memory, on disk, or in its event log."""

    if (tenant_id, run_id) in ctx.runs:
        return True
    if ctx.events.exists(tenant_id, run_id):
        return True
    return ctx.load_from_checkpoints(ctx.root, tenant_id, run_id) is not None


def register_library_routes(app: FastAPI, rs: RouteState) -> None:
    ctx = rs.ctx
    require_runs = rs.require_runs
    require_metrics = rs.require_metrics
    _optional_console_user = rs.optional_console_user
    _resolve_tenant = rs.resolve_tenant
    _require_library_write = rs.require_library_write

    # ----- C0 goals / templates -----
    @app.post("/v1/goals/preview")
    def goals_preview(body: GoalPreview, principal=Depends(require_runs)) -> dict[str, Any]:
        return ctx.library_service.preview_goal(principal, goal=body.goal)

    @app.post("/v1/goals/suggest")
    def goals_suggest(body: GoalSuggest, principal=Depends(require_runs)) -> dict[str, Any]:
        """Pilot Compose: draft desired states (never locks criteria)."""

        return ctx.library_service.suggest_goal(
            principal,
            context=body.context,
            task_class=body.task_class or "repo-chore",
            use_model=body.use_model,
        )

    @app.post("/v1/goals/probe")
    def goals_probe(
        body: GoalProbe,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        """Read-only inventory assist for Compose / programs (never locks criteria)."""

        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        rel = body.workdir.strip().lstrip("/")
        if ".." in Path(rel).parts or Path(rel).is_absolute():
            raise HTTPException(status_code=400, detail="workdir must be relative")
        root = (ctx.root / "workspaces" / tenant_id / rel).resolve()
        try:
            root.relative_to((ctx.root / "workspaces" / tenant_id).resolve())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="workdir escapes tenant root") from exc
        return ctx.library_service.probe_goal(principal, workdir=root)

    @app.get("/v1/templates")
    def templates(principal=Depends(require_runs)) -> dict[str, Any]:
        return ctx.library_service.list_templates(principal)

    @app.get("/v1/templates/{template_id}")
    def template_detail(template_id: str, principal=Depends(require_runs)) -> dict[str, Any]:
        try:
            return ctx.library_service.get_template(principal, template_id)
        except NotFound as exc:
            raise HTTPException(status_code=404, detail=exc.message) from exc

    # ----- C0 runs list / transcript / trajectory -----
    @app.get("/v1/runs")
    def list_runs(
        request: Request,
        principal=Depends(require_runs),
        task_class: str | None = None,
        terminal: str | None = None,
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = None,
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        return ctx.run_service.list(
            tenant_id,
            runs_root=ctx.tenant_runs_root(tenant_id),
            limit=limit,
            cursor=cursor,
            task_class=task_class,
            terminal=terminal,
        )

    @app.get("/v1/runs/{run_id}/transcript")
    def run_transcript(
        run_id: str,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        run_id = ctx.validate_run_id(run_id)
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        latest = ctx.checkpoint_reader.latest_row(
            tenant_id, run_id, runs_root=ctx.tenant_runs_root(tenant_id)
        )
        if latest is None:
            raise HTTPException(status_code=404, detail="run not found")
        state = latest[3]
        ref = state.transcript_ref
        store = TranscriptStore(ctx.tenant_runs_root(tenant_id) / "transcripts")
        if not ref:
            return {"run_id": run_id, "events": [], "content_hash": None}
        try:
            payload = store.read(ref)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="transcript not found") from exc
        return {"run_id": run_id, "content_hash": ref, **payload}

    @app.get("/v1/runs/{run_id}/trajectory")
    def run_trajectory(
        run_id: str,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        run_id = ctx.validate_run_id(run_id)
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        store = TrajectoryStore(ctx.tenant_runs_root(tenant_id) / "trajectories")
        traj = store.get_trajectory(run_id)
        if traj is None:
            raise HTTPException(status_code=404, detail="trajectory not found")
        return traj.model_dump(mode="json")

    # ----- C2 async / events / cancel -----
    @app.get("/v1/runs/{run_id}/events")
    def run_events(
        run_id: str,
        request: Request,
        principal=Depends(require_runs),
        after: str | None = None,
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> StreamingResponse:
        run_id = ctx.validate_run_id(run_id)
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        if not _tenant_owns_run(ctx, tenant_id, run_id):
            raise HTTPException(status_code=404, detail="run not found")

        def gen():
            yield from ctx.events.iter_sse(tenant_id, run_id, after=after)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/v1/runs/{run_id}/cancel")
    def cancel_run(
        run_id: str,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        run_id = ctx.validate_run_id(run_id)
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        if not _tenant_owns_run(ctx, tenant_id, run_id):
            raise HTTPException(status_code=404, detail="run not found")
        ctx.worker.request_cancel(tenant_id, run_id)
        ctx.events.append(tenant_id, run_id, "run.cancelled", {"by": principal.key_id})
        return {"run_id": run_id, "status": "cancel_requested"}

    # ----- C0 skills -----
    @app.get("/v1/skills")
    def list_skills(
        request: Request,
        principal=Depends(require_runs),
        task_class: str | None = None,
        lifecycle: str | None = None,
        active: bool | None = None,
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        store = SkillStore(ctx.tenant_skills_root(tenant_id))
        eval_db = ctx.tenant_runs_root(tenant_id) / "evals.db"
        eval_store = EvalStore(eval_db) if eval_db.exists() else None
        try:
            items = ctx.library_service.list_skills(
                principal,
                store=store,
                eval_store=eval_store,
                task_class=task_class,
                lifecycle=lifecycle,
                active=active,
            )
        finally:
            if eval_store is not None:
                eval_store.close()
        return {"items": items}

    @app.get("/v1/skills/{skill_id}/versions/{version}")
    def skill_version(
        skill_id: str,
        version: int,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        store = SkillStore(ctx.tenant_skills_root(tenant_id))
        eval_db = ctx.tenant_runs_root(tenant_id) / "evals.db"
        eval_store = EvalStore(eval_db) if eval_db.exists() else None
        try:
            return ctx.library_service.get_skill(
                principal,
                store=store,
                skill_id=skill_id,
                version=version,
                eval_store=eval_store,
                include_identity=True,
            )
        except NotFound as exc:
            raise HTTPException(status_code=404, detail="skill not found") from exc
        finally:
            if eval_store is not None:
                eval_store.close()

    @app.post("/v1/skills/search")
    def skills_search(
        payload: dict[str, Any],
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        query = str(payload.get("query") or "")
        limit = int(payload.get("limit") or 5)
        store = SkillStore(ctx.tenant_skills_root(tenant_id))
        index_path = ctx.tenant_runs_root(tenant_id) / "skill_index.db"
        from recertia.retrieval.index import SkillIndex

        index = SkillIndex(index_path)
        return ctx.library_service.search_skills(
            principal, store=store, index=index, query=query, limit=limit
        )

    @app.post("/v1/skills/{skill_id}/versions/{version}/promote")
    def promote_skill(
        skill_id: str,
        version: int,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        _require_library_write(request, principal, scope="promote", min_role="reviewer")
        user = _optional_console_user(request)
        job = ctx.job_runs.create(
            "promote",
            tenant_id=tenant_id,
            dry_run=False,
            meta={"skill_id": skill_id, "version": version},
        )
        job.status = "running"
        ctx.job_runs.save(job)
        store = SkillStore(ctx.tenant_skills_root(tenant_id))
        runs_root = ctx.tenant_runs_root(tenant_id)
        log_dir = runs_root / "promotion_logs"
        try:
            status = ctx.promotion_service.promote(
                principal,
                skill_id,
                version,
                store=store,
                golden_root=Path("evals/golden"),
                runs_root=runs_root,
                log_dir=log_dir,
                require_task_class_gate=False,
                golden_dir=Path("evals/golden") / "repo-chore" / skill_id,
                console_roles=frozenset(user.roles) if user is not None else None,
            )
            if status.lifecycle != "approved":
                raise PromotionError(
                    f"promote refused: lifecycle={status.lifecycle!r} (golden gate required)"
                )
            job.status = "succeeded"
            job.finished_at = datetime.now(timezone.utc).isoformat()
            job.meta["lifecycle"] = status.lifecycle
        except PromotionError as exc:
            job.status = "failed"
            job.error = str(exc)
            job.finished_at = datetime.now(timezone.utc).isoformat()
            job.meta["failing_fixtures"] = list(exc.failing_fixtures)
            ctx.job_runs.save(job)
            return {
                "job_run_id": job.job_run_id,
                "status": "failed",
                "error": str(exc),
                "failing_fixtures": list(exc.failing_fixtures),
            }
        except Forbidden as exc:
            job.status = "failed"
            job.error = exc.message
            job.finished_at = datetime.now(timezone.utc).isoformat()
            ctx.job_runs.save(job)
            raise HTTPException(status_code=403, detail=exc.message) from exc
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.error = str(exc)
            job.finished_at = datetime.now(timezone.utc).isoformat()
            ctx.job_runs.save(job)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ctx.job_runs.save(job)
        ledger = HashChainLedger(runs_root / "ledger.jsonl")
        ledger.append(
            actor=(user.user_id if user else ledger_actor(principal)),
            action="policy_change",
            target=f"skill:{skill_id}@v{version}",
            evidence={"kind": "console_promote", "job_run_id": job.job_run_id},
        )
        return {
            "job_run_id": job.job_run_id,
            "status": "succeeded",
            "lifecycle": "approved",
            "active": status.active,
        }

    # ----- C0 metrics -----
    @app.get("/v1/metrics/report")
    def metrics_report(
        request: Request,
        principal=Depends(require_metrics),
        task_class: str = "repo-chore",
        snapshot_id: str | None = None,
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        eval_db = ctx.tenant_runs_root(tenant_id) / "evals.db"
        store = EvalStore(eval_db)
        try:
            skill_store = SkillStore(ctx.tenant_skills_root(tenant_id))
            report = ctx.eval_service.metrics_report(
                principal,
                eval_store=store,
                skill_store=skill_store,
                task_class=task_class,
                snapshot_id=snapshot_id,
                tenant_id=tenant_id,
            )
        finally:
            store.close()
        return report.model_dump(mode="json")

    @app.get("/v1/metrics/canary")
    def metrics_canary(principal=Depends(require_metrics)) -> dict[str, Any]:
        report = ctx.eval_service.run_canary(principal)
        return {
            "trials": report.trials,
            "false_passes": report.false_passes,
            "false_pass_rate": report.false_pass_rate,
            "model_version": report.model_version,
        }

    @app.get("/v1/ledger/verify")
    def ledger_verify(
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        path = ctx.tenant_runs_root(tenant_id) / "ledger.jsonl"
        ledger = HashChainLedger(path)
        try:
            from recertia.settings import load_settings

            full = load_settings().ledger_verify_full
            report = ctx.library_service.verify_ledger(principal, ledger=ledger, full=full)
            return {
                "ok": report.ok,
                "entries": report.entries,
                "full": report.full,
                "suffix_entries": report.suffix_entries,
                "tip_hash": report.tip_hash,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    # ----- C1 proposals / jobs -----
    @app.get("/v1/proposals")
    def list_proposals(
        request: Request,
        principal=Depends(require_runs),
        status: str | None = None,
        kind: str | None = None,
        limit: int = 50,
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        items = ctx.library_service.list_proposals(
            principal,
            store=ctx.proposals,
            tenant_id=tenant_id,
            status=status,
            kind=kind,
            limit=limit,
        )
        return {"items": [p.to_dict() for p in items]}

    @app.get("/v1/proposals/{proposal_id}")
    def get_proposal(
        proposal_id: str,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        try:
            rec = ctx.library_service.get_proposal(
                principal, store=ctx.proposals, proposal_id=proposal_id, tenant_id=tenant_id
            )
        except NotFound as exc:
            raise HTTPException(status_code=404, detail=exc.message) from exc
        return rec.to_dict()

    @app.post("/v1/proposals/{proposal_id}/decision")
    def decide_proposal(
        proposal_id: str,
        body: ProposalDecision,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        user = _optional_console_user(request)
        actor = user.user_id if user else ledger_actor(principal)
        ledger = HashChainLedger(ctx.tenant_runs_root(tenant_id) / "ledger.jsonl")
        try:
            updated = ctx.library_service.decide_proposal(
                principal,
                store=ctx.proposals,
                proposal_id=proposal_id,
                tenant_id=tenant_id,
                decision=body.decision,
                actor=actor,
                note=body.note,
                ledger=ledger,
                console_may_review=user.may("reviewer") if user is not None else None,
            )
        except NotFound as exc:
            raise HTTPException(status_code=404, detail=exc.message) from exc
        except Forbidden as exc:
            raise HTTPException(status_code=403, detail=exc.message) from exc
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc
        return updated.to_dict()

    @app.get("/v1/jobs")
    def list_jobs(
        request: Request,
        principal=Depends(require_runs),
        limit: int = 50,
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        return {
            "items": [j.to_dict() for j in ctx.job_runs.list(tenant_id=tenant_id, limit=limit)]
        }

    @app.get("/v1/jobs/{job_run_id}")
    def get_job(
        job_run_id: str,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        rec = ctx.job_runs.get(job_run_id, tenant_id=tenant_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="job not found")
        return rec.to_dict()

    @app.post("/v1/jobs/{job}/run")
    def trigger_job(
        job: str,
        body: JobTrigger,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        _require_library_write(request, principal, scope="jobs", min_role="reviewer")
        from recertia.memory.procedural.lineage import LineageServices
        from recertia.policy_load import load_policy

        user = _optional_console_user(request)
        policy = load_policy()
        runs_root = ctx.tenant_runs_root(tenant_id)
        lineage = LineageServices.open(runs_root / "lineage")
        store = SkillStore(
            ctx.tenant_skills_root(tenant_id),
            lineage_index=lineage.index,
            revoke_queue=lineage.queue,
        )
        runner = build_job_runner(store, runs_root=runs_root / "jobs", policy=policy)
        attach_enablement(
            runner,
            eval_db=runs_root / "evals.db",
            skills_root=ctx.tenant_skills_root(tenant_id),
        )
        budget = JobBudget(max_proposals=body.max_proposals)
        resolved = ctx.job_service.resolve(job)
        job_rec = ctx.job_runs.create(
            resolved.name if resolved is not None else job,
            tenant_id=tenant_id,
            dry_run=body.dry_run,
        )
        job_rec.status = "running"
        ctx.job_runs.save(job_rec)
        try:
            result = ctx.job_service.run(
                principal,
                job,
                dispatch=JobDispatch(
                    store=store,
                    runner=runner,
                    budget=budget,
                    policy=policy,
                    runs_root=runs_root,
                    lineage=lineage,
                    dry_run=body.dry_run,
                    one_off=list(body.one_off) if body.one_off else None,
                    tool_upgraded=body.tool_upgraded,
                    hint=list(body.hint) if body.hint else None,
                    skill_id=body.skill_id,
                    skill_version=body.skill_version,
                    fake_edge_failures=body.fake_edge_failures,
                    merge_conflicts=body.merge_conflicts,
                ),
                console_roles=frozenset(user.roles) if user is not None else None,
            )
        except HTTPException:
            raise
        except NotFound as exc:
            job_rec.status = "failed"
            job_rec.error = exc.message
            job_rec.finished_at = datetime.now(timezone.utc).isoformat()
            ctx.job_runs.save(job_rec)
            raise HTTPException(status_code=404, detail=exc.message) from exc
        except Forbidden as exc:
            job_rec.status = "failed"
            job_rec.error = exc.message
            job_rec.finished_at = datetime.now(timezone.utc).isoformat()
            ctx.job_runs.save(job_rec)
            raise HTTPException(status_code=403, detail=exc.message) from exc
        except ConfigError as exc:
            job_rec.status = "failed"
            job_rec.error = exc.message
            job_rec.finished_at = datetime.now(timezone.utc).isoformat()
            ctx.job_runs.save(job_rec)
            raise HTTPException(status_code=400, detail=exc.message) from exc
        except Exception as exc:  # noqa: BLE001
            job_rec.status = "failed"
            job_rec.error = str(exc)
            job_rec.finished_at = datetime.now(timezone.utc).isoformat()
            ctx.job_runs.save(job_rec)
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        persisted = []
        for p in result.proposals:
            rec = ProposalRecord(
                proposal_id=uuid4().hex[:12],
                kind=p.kind,
                skill_id=p.skill_id,
                version=p.version,
                rationale=p.rationale,
                payload=p.payload,
                tenant_id=tenant_id,
                created_by_job=job_rec.job_run_id,
            )
            if not body.dry_run:
                ctx.proposals.add(rec)
            persisted.append(rec.to_dict())
        job_rec.status = "succeeded"
        job_rec.proposals = persisted
        job_rec.finished_at = datetime.now(timezone.utc).isoformat()
        ctx.job_runs.save(job_rec)
        return job_rec.to_dict()

    @app.get("/v1/console/tower-summary")
    def tower_summary(
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        """C4: practice conversion + active cap pressure for Tower panels."""

        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        skill_store = SkillStore(ctx.tenant_skills_root(tenant_id))
        eval_db = ctx.tenant_runs_root(tenant_id) / "evals.db"
        eval_store = EvalStore(eval_db) if eval_db.exists() else None
        try:
            return ctx.library_service.tower_summary(
                principal,
                skill_store=skill_store,
                eval_store=eval_store,
                proposals=ctx.proposals,
                tenant_id=tenant_id,
            )
        finally:
            if eval_store is not None:
                eval_store.close()

