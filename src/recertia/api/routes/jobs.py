"""HTTP adapters. Handlers map RecertiaError to HTTP; services own policy."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from recertia.api.deps import RouteState
from recertia.api.schemas import JobTrigger
from recertia.app.jobs import JobDispatch
from recertia.errors import ConfigError, Forbidden, NotFound
from recertia.jobs import JobBudget, build_job_runner
from recertia.jobs.enablement import attach_enablement
from recertia.memory.procedural.store import SkillStore
from recertia.policy_load import load_policy
from recertia.proposals.store import ProposalRecord


def register_jobs_routes(app: FastAPI, rs: RouteState) -> None:
    ctx = rs.ctx
    require_runs = rs.require_runs
    _optional_console_user = rs.optional_console_user
    _resolve_tenant = rs.resolve_tenant
    _require_library_write = rs.require_library_write
    _public_user = rs.public_user

    @app.get("/v1/jobs")
    def list_jobs(
        request: Request,
        principal=Depends(require_runs),
        limit: int = 50,
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        return {
            "items": [
                j.public_dict() for j in ctx.job_runs.list(tenant_id=tenant_id, limit=limit)
            ]
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
        return rec.public_dict()
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
        try:
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
        finally:
            runner.close()

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

