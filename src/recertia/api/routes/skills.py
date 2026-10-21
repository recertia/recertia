"""HTTP adapters. Handlers map RecertiaError to HTTP; services own policy."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from recertia.api.deps import RouteState
from recertia.errors import Forbidden, NotFound
from recertia.evals.store import EvalStore
from recertia.ledger import HashChainLedger
from recertia.memory.procedural.promote import PromotionError
from recertia.memory.procedural.store import SkillStore
from recertia.ports.identity import ledger_actor
from recertia.review.promotion import REFUSAL_AUDIT_UNAVAILABLE
from recertia.review.promotion_audit import open_operator_audit

_AUDIT_UNAVAILABLE_MESSAGE = (
    "promotion refused: operator audit store unavailable; the golden gate did not run"
)


def _finish_failed(ctx: Any, job: Any, error: str) -> None:
    job.status = "failed"
    job.error = error
    job.finished_at = datetime.now(timezone.utc).isoformat()
    ctx.job_runs.save(job)


def _audit_unavailable_response(ctx: Any, job: Any, message: str) -> JSONResponse:
    """503, decision-only; the job is marked failed (never left ``running``)."""

    decision = {
        "decision": "refused",
        "refusal_kind": REFUSAL_AUDIT_UNAVAILABLE,
        "failing_count": 0,
        "audit_ref": None,
    }
    job.meta.update(decision)
    _finish_failed(ctx, job, message)
    return JSONResponse(
        status_code=503,
        content={"job_run_id": job.job_run_id, "status": "failed", "error": message, **decision},
    )


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
    ) -> Any:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        _require_library_write(request, principal, scope="promote", min_role="reviewer")
        user = _optional_console_user(request)
        job = ctx.job_runs.create(
            "promote",
            tenant_id=tenant_id,
            dry_run=False,
            meta={"skill_id": skill_id, "version": version, "decision_only": True},
        )
        job.status = "running"
        ctx.job_runs.save(job)
        store = SkillStore(ctx.tenant_skills_root(tenant_id))
        runs_root = ctx.tenant_runs_root(tenant_id)
        log_dir = runs_root / "promotion_logs"
        # REC-009: refusal detail goes to the operator-only sink, never to the job record,
        # the response or the tenant runs_root. Fail closed: an unset, misplaced or broken
        # sink refuses the promotion before the golden gate runs.
        try:
            audit = open_operator_audit(
                getattr(ctx.settings, "operator_audit_root", ""),
                forbidden_roots=[ctx.root, runs_root],
            )
        except Exception:  # noqa: BLE001 - AuditSinkUnavailable or any open failure
            return _audit_unavailable_response(ctx, job, _AUDIT_UNAVAILABLE_MESSAGE)
        detail: str | None = None
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
                audit=audit,
            )
            if status.lifecycle != "approved":
                raise PromotionError(
                    f"promote refused: lifecycle={status.lifecycle!r} (golden gate required)"
                )
            job.status = "succeeded"
            job.finished_at = datetime.now(timezone.utc).isoformat()
            job.meta["lifecycle"] = status.lifecycle
        except PromotionError as exc:
            if exc.refusal_kind == REFUSAL_AUDIT_UNAVAILABLE:
                return _audit_unavailable_response(ctx, job, str(exc))
            decision = exc.decision()
            job.meta.update(decision)
            _finish_failed(ctx, job, str(exc))
            return {
                "job_run_id": job.job_run_id,
                "status": "failed",
                "error": str(exc),
                **decision,
            }
        except Forbidden as exc:
            _finish_failed(ctx, job, exc.message)
            raise HTTPException(status_code=403, detail=exc.message) from exc
        except Exception as exc:  # noqa: BLE001
            # An unexpected error can carry fixture ids, paths or contents: the text goes
            # to the operator sink only, never to the body, the job record or the log.
            detail = f"{type(exc).__name__}: {exc}"
        if detail is not None:
            audit_ref: str | None = None
            try:
                audit_ref = audit.record(
                    {
                        "skill_id": skill_id,
                        "version": version,
                        "refusal_kind": "internal_error",
                        "detail": detail,
                    }
                )
            except Exception:  # noqa: BLE001 - a sink failure fails closed
                audit_ref = None
            if not audit_ref:
                return _audit_unavailable_response(
                    ctx,
                    job,
                    "promotion failed: internal error; operator audit store unavailable",
                )
            message = f"promotion failed: internal error; operator detail: audit_ref={audit_ref}"
            job.meta["audit_ref"] = audit_ref
            _finish_failed(ctx, job, message)
            raise HTTPException(status_code=400, detail=message)
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

