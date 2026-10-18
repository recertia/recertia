"""HTTP adapters. Handlers map RecertiaError to HTTP; services own policy."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from recertia.api.deps import RouteState
from recertia.errors import Forbidden, NotFound
from recertia.evals.store import EvalStore
from recertia.ledger import HashChainLedger
from recertia.memory.procedural.promote import PromotionError
from recertia.memory.procedural.store import SkillStore
from recertia.ports.identity import ledger_actor


def register_skills_routes(app: FastAPI, rs: RouteState) -> None:
    ctx = rs.ctx
    require_runs = rs.require_runs
    _optional_console_user = rs.optional_console_user
    _resolve_tenant = rs.resolve_tenant
    _require_library_write = rs.require_library_write
    _public_user = rs.public_user

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

