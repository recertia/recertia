"""Console library routes: goals, templates, runs, skills, metrics, proposals, jobs, tower."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from contracts.goal import compile_goal
from recertia.api.console_deps import RouteState
from recertia.api.console_routes import (
    GoalPreview,
    GoalProbe,
    GoalSuggest,
    JobTrigger,
    ProposalDecision,
)
from recertia.console_compose import suggest_criteria
from recertia.console_templates import get_template_goal, list_templates
from recertia.evals.canary import run_judge_canary
from recertia.evals.metrics import build_metric_report
from recertia.evals.report import assemble_metric_report
from recertia.evals.store import EvalStore
from recertia.graph.engine import GraphOrchestrator
from recertia.jobs.dispatch import JobDispatchError, JobRequest, UnknownJob, execute_job
from recertia.ledger import HashChainLedger
from recertia.memory.procedural.active_set import recompute_active_set
from recertia.memory.procedural.composition import mean_composition_depth
from recertia.memory.procedural.promote import PromotionError, promote_to_approved
from recertia.memory.procedural.store import SkillStore
from recertia.proposals.store import ProposalRecord
from recertia.review.autonomy_config import DEFAULT_AUTONOMY
from recertia.solver.transcript import TranscriptStore
from recertia.trajectory.store import TrajectoryStore


def register_library_routes(app: FastAPI, rs: RouteState) -> None:
    ctx = rs.ctx
    require_runs = rs.require_runs
    require_metrics = rs.require_metrics
    _optional_console_user = rs.optional_console_user
    _resolve_tenant = rs.resolve_tenant
    _require_workspace_admin = rs.require_workspace_admin
    _require_library_write = rs.require_library_write
    _public_user = rs.public_user

    # ----- C0 goals / templates -----
    @app.post("/v1/goals/preview")
    def goals_preview(body: GoalPreview, principal=Depends(require_runs)) -> dict[str, Any]:
        del principal
        criteria = compile_goal(body.goal)
        return {
            "goal": body.goal.model_dump(mode="json"),
            "criteria": [c.model_dump(mode="json") for c in criteria],
        }

    @app.post("/v1/goals/suggest")
    def goals_suggest(body: GoalSuggest, principal=Depends(require_runs)) -> dict[str, Any]:
        """Pilot Compose: draft desired states (never locks criteria)."""

        del principal
        result = suggest_criteria(
            context=body.context,
            task_class=body.task_class or "repo-chore",
            use_model=body.use_model,
        )
        payload = result.to_dict()
        payload["blocked"] = any(w["severity"] == "block" for w in payload["warnings"])
        return payload

    @app.post("/v1/goals/probe")
    def goals_probe(
        body: GoalProbe,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        """Read-only inventory assist for Compose / programs (never locks criteria)."""

        from recertia.programs.probe import probe_workdir

        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        rel = body.workdir.strip().lstrip("/")
        if ".." in Path(rel).parts or Path(rel).is_absolute():
            raise HTTPException(status_code=400, detail="workdir must be relative")
        root = (ctx.root / "workspaces" / tenant_id / rel).resolve()
        try:
            root.relative_to((ctx.root / "workspaces" / tenant_id).resolve())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="workdir escapes tenant root") from exc
        return {"probe": probe_workdir(root), "locked": False}

    @app.get("/v1/templates")
    def templates(principal=Depends(require_runs)) -> dict[str, Any]:
        del principal
        return {"templates": list_templates()}

    @app.get("/v1/templates/{template_id}")
    def template_detail(template_id: str, principal=Depends(require_runs)) -> dict[str, Any]:
        del principal
        try:
            goal = get_template_goal(template_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="template not found") from exc
        return {"id": template_id, "goal": goal.model_dump(mode="json")}

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
        orch = GraphOrchestrator(ctx.tenant_runs_root(tenant_id))
        try:
            ids = orch.checkpoints.list_run_ids()
        finally:
            orch.close()
        # Also include in-memory runs for this tenant.
        mem_ids = [rid for (tid, rid) in ctx.runs if tid == tenant_id]
        all_ids = sorted(set(ids) | set(mem_ids))
        if cursor:
            all_ids = [i for i in all_ids if i > cursor]
        items: list[dict[str, Any]] = []
        for run_id in all_ids:
            if len(items) >= limit:
                break
            rec = ctx.runs.get((tenant_id, run_id)) or ctx.load_from_checkpoints(
                ctx.root, tenant_id, run_id
            )
            if rec is None:
                continue
            if task_class and rec.task_class != task_class:
                continue
            if terminal and (rec.terminal or "") != terminal:
                continue
            # PC-1: never leak other tenants — record carries tenant_id
            if rec.tenant_id != tenant_id:
                continue
            items.append(
                {
                    "run_id": rec.run_id,
                    "task_class": rec.task_class,
                    "terminal": rec.terminal,
                    "status": rec.status,
                    "attempt_no": rec.attempt_no,
                    "created_at": rec.created_at.isoformat()
                    if hasattr(rec.created_at, "isoformat")
                    else rec.created_at,
                    "cost_usd": getattr(rec, "cost_usd", None),
                    "arm": rec.arm,
                    "tenant_id": rec.tenant_id,
                }
            )
        next_cursor = items[-1]["run_id"] if items and len(items) == limit else None
        return {"items": items, "next_cursor": next_cursor}

    @app.get("/v1/runs/{run_id}/transcript")
    def run_transcript(
        run_id: str,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        run_id = ctx.validate_run_id(run_id)
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        orch = GraphOrchestrator(ctx.tenant_runs_root(tenant_id))
        try:
            latest = orch.checkpoints.latest(run_id)
            if latest is None:
                raise HTTPException(status_code=404, detail="run not found")
            state = latest[3]
        finally:
            orch.close()
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
        rec = ctx.runs.get((tenant_id, run_id)) or ctx.load_from_checkpoints(
            ctx.root, tenant_id, run_id
        )
        if rec is None and not (ctx.root / "run_events" / f"{run_id}.jsonl").exists():
            raise HTTPException(status_code=404, detail="run not found")

        def gen():
            yield from ctx.events.iter_sse(run_id, after=after)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/v1/runs/{run_id}/cancel")
    def cancel_run(
        run_id: str,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        del x_recertia_tenant, request
        run_id = ctx.validate_run_id(run_id)
        ctx.worker.request_cancel(run_id)
        ctx.events.append(run_id, "run.cancelled", {"by": principal.key_id})
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
        streaks: dict[tuple[str, int], int] = {}
        eval_db = ctx.tenant_runs_root(tenant_id) / "evals.db"
        if eval_db.exists():
            eval_store = EvalStore(eval_db)
            try:
                streaks = eval_store.field_failure_streaks()
            finally:
                eval_store.close()
        from recertia.memory.procedural.live_mix import live_mix_view

        items = []
        for ver, status, stats in store.iter_loaded():
            if task_class and ver.task_class != task_class:
                continue
            if lifecycle and status.lifecycle != lifecycle:
                continue
            if active is not None and status.active != active:
                continue
            streak = streaks.get((ver.skill_id, ver.version), 0)
            items.append(
                {
                    "skill_id": ver.skill_id,
                    "version": ver.version,
                    "title": ver.title,
                    "task_class": ver.task_class,
                    "lifecycle": status.lifecycle,
                    "active": status.active,
                    "live_mix": live_mix_view(
                        ver, status, stats, consecutive_field_failures=streak
                    ),
                    "contribution": stats.contribution.model_dump(mode="json")
                    if stats.contribution
                    else None,
                }
            )
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
        try:
            ver = store.get_version(skill_id, version)
            status = store.get_status(skill_id, version)
            stats = store.get_stats(skill_id, version)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=404, detail="skill not found") from exc
        from recertia.memory.procedural.apply_diversity import skill_identity
        from recertia.memory.procedural.live_mix import live_mix_view

        streak = 0
        eval_db = ctx.tenant_runs_root(tenant_id) / "evals.db"
        if eval_db.exists():
            eval_store = EvalStore(eval_db)
            try:
                streak = eval_store.field_failure_streaks().get((skill_id, version), 0)
            finally:
                eval_store.close()
        return {
            "version": ver.model_dump(mode="json"),
            "status": status.model_dump(mode="json"),
            "stats": stats.model_dump(mode="json"),
            "identity": skill_identity(ver, stats),
            "live_mix": live_mix_view(
                ver, status, stats, consecutive_field_failures=streak
            ),
        }

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
        try:
            entries = list(store.iter_loaded())
            index.rebuild(entries, library_fingerprint=store.library_fingerprint())
            hits = index.lexical_top_k(query, limit)
        finally:
            if hasattr(index, "close"):
                index.close()  # type: ignore[attr-defined]
        return {
            "query": query,
            "hits": [
                {"skill_id": sid, "version": ver, "score": score} for sid, ver, score in hits
            ],
        }

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
            status = promote_to_approved(
                store,
                skill_id,
                version,
                golden_root=Path("evals/golden"),
                runs_root=runs_root,
                log_dir=log_dir,
                require_task_class_gate=False,
                golden_dir=Path("evals/golden") / "repo-chore" / skill_id,
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
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.error = str(exc)
            job.finished_at = datetime.now(timezone.utc).isoformat()
            ctx.job_runs.save(job)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ctx.job_runs.save(job)
        ledger = HashChainLedger(runs_root / "ledger.jsonl")
        ledger.append(
            actor=(user.user_id if user else principal.key_id),
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
            report = assemble_metric_report(
                store,
                skill_store=skill_store,
                task_class=task_class,
                snapshot_id=snapshot_id,
            )
        finally:
            store.close()
        return report.model_dump(mode="json")

    @app.get("/v1/metrics/canary")
    def metrics_canary(principal=Depends(require_metrics)) -> dict[str, Any]:
        del principal
        report = run_judge_canary()
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
            ledger.verify()
            return {"ok": True, "entries": len(ledger.entries())}
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
        items = ctx.proposals.list(
            tenant_id=tenant_id, status=status, kind=kind, limit=limit
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
        rec = ctx.proposals.get(proposal_id, tenant_id=tenant_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="proposal not found")
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
        rec = ctx.proposals.get(proposal_id, tenant_id=tenant_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        if rec.kind in {"correction"} or rec.payload.get("tier") == "T2":
            if user is not None and not user.may("reviewer"):
                raise HTTPException(status_code=403, detail="T2 requires reviewer")
            if user is None and "admin" not in principal.scopes:
                raise HTTPException(status_code=403, detail="T2 requires admin key or reviewer")
        actor = user.user_id if user else principal.key_id
        try:
            updated = ctx.proposals.decide(
                proposal_id,
                tenant_id=tenant_id,
                decision=body.decision,
                actor=actor,
                note=body.note,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ledger = HashChainLedger(ctx.tenant_runs_root(tenant_id) / "ledger.jsonl")
        ledger.append(
            actor=actor,
            action="policy_change",
            target=f"proposal:{proposal_id}",
            evidence={
                "kind": "proposal_decision",
                "decision": body.decision,
                "note": body.note,
                "proposal_kind": rec.kind,
            },
        )
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
        name = job.strip().lower()
        from recertia.memory.procedural.lineage import LineageServices
        from recertia.policy_load import load_policy

        policy = load_policy()
        runs_root = ctx.tenant_runs_root(tenant_id)
        lineage = LineageServices.open(runs_root / "lineage")
        store = SkillStore(
            ctx.tenant_skills_root(tenant_id),
            lineage_index=lineage.index,
            revoke_queue=lineage.queue,
        )
        job_rec = ctx.job_runs.create(
            name, tenant_id=tenant_id, dry_run=body.dry_run
        )
        job_rec.status = "running"
        ctx.job_runs.save(job_rec)
        edits_log = None
        if body.edits_log:
            rel = Path(body.edits_log)
            if rel.is_absolute() or ".." in rel.parts:
                job_rec.status = "failed"
                job_rec.error = "edits_log must be a relative path"
                job_rec.finished_at = datetime.now(timezone.utc).isoformat()
                ctx.job_runs.save(job_rec)
                raise HTTPException(
                    status_code=400, detail="edits_log must be a relative path"
                )
            edits_log = runs_root / rel
        try:
            result = execute_job(
                JobRequest(
                    name=name,
                    dry_run=body.dry_run,
                    max_proposals=body.max_proposals,
                    max_tokens=body.max_tokens,
                    task_class=body.task_class,
                    hint=list(body.hint) if body.hint else None,
                    arxiv_id=list(body.arxiv_id) if body.arxiv_id else None,
                    arxiv_query=body.arxiv_query,
                    arxiv_max=body.arxiv_max,
                    with_pdf=body.with_pdf,
                    pdf_sandbox=body.pdf_sandbox,
                    one_off=list(body.one_off) if body.one_off else None,
                    tool_upgraded=body.tool_upgraded,
                    skill_id=body.skill_id,
                    skill_version=body.skill_version,
                    fake_edge_failures=body.fake_edge_failures,
                    merge_conflicts=body.merge_conflicts,
                    edits_log=edits_log,
                ),
                store=store,
                runs_root=runs_root,
                skills_root=ctx.tenant_skills_root(tenant_id),
                policy=policy,
            )
        except UnknownJob as exc:
            job_rec.status = "failed"
            job_rec.error = str(exc)
            job_rec.finished_at = datetime.now(timezone.utc).isoformat()
            ctx.job_runs.save(job_rec)
            raise HTTPException(status_code=404, detail=f"unknown job {job}") from exc
        except JobDispatchError as exc:
            job_rec.status = "failed"
            job_rec.error = str(exc)
            job_rec.finished_at = datetime.now(timezone.utc).isoformat()
            ctx.job_runs.save(job_rec)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            job_rec.status = "failed"
            job_rec.error = str(exc)
            job_rec.finished_at = datetime.now(timezone.utc).isoformat()
            ctx.job_runs.save(job_rec)
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        persisted = []
        for p in result.proposals:
            payload = dict(p.payload or {})
            payload.pop("_pdf_text", None)
            rec = ProposalRecord(
                proposal_id=uuid4().hex[:12],
                kind=p.kind,
                skill_id=p.skill_id,
                version=p.version,
                rationale=p.rationale,
                payload=payload,
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
        _u, pressure = recompute_active_set(skill_store, config=DEFAULT_AUTONOMY)
        mean_pressure = sum(pressure.values()) / len(pressure) if pressure else 0.0
        eval_db = ctx.tenant_runs_root(tenant_id) / "evals.db"
        practice_conversion = None
        unavailable = None
        if eval_db.exists():
            store = EvalStore(eval_db)
            try:
                rows = store.metric_rows()
                report = build_metric_report(
                    rows,
                    snapshot_id="tower",
                    active_cap_pressure=mean_pressure,
                    mean_composition_depth=mean_composition_depth(skill_store),
                )
                practice_conversion = report.practice_conversion
                unavailable = report.unavailable.get("practice_conversion")
            finally:
                store.close()
        pending = ctx.proposals.list(tenant_id=tenant_id, status="pending", limit=100)
        return {
            "active_cap_pressure": mean_pressure,
            "pressure_by_class": pressure,
            "mean_composition_depth": mean_composition_depth(skill_store),
            "practice_conversion": practice_conversion,
            "practice_conversion_unavailable": unavailable,
            "pending_proposals": len(pending),
        }

