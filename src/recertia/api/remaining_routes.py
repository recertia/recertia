"""Remaining HTTP surface (RW-SUR): evals, policy, reviews alias, memory planes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, Request
from pydantic import BaseModel, Field

from recertia.api.console_auth import console_session
from recertia.api.console_routes import ConsoleContext
from recertia.api.errors import V1HTTPError
from recertia.errors import ConfigError, Forbidden, NotFound
from recertia.evals.store import EvalStore
from recertia.ledger import HashChainLedger
from recertia.memory.affordance import AffordanceStore
from recertia.memory.episodic import EpisodicStore
from recertia.memory.semantic import FactStore
from recertia.paths import PathEscapeError, contained_path, looks_absolute
from recertia.ports.identity import ledger_actor


class EvalRunBody(BaseModel):
    task_class: str = "repo-chore"
    snapshot: str | None = None
    golden_dir: str | None = None
    golden_root: str | None = None


class PolicyProposalBody(BaseModel):
    policy_diff: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""


class ReviewDecisionBody(BaseModel):
    decision: str
    note: str = ""


class DistillTrajectoryBody(BaseModel):
    task_class: str
    trajectory: dict[str, Any]


class MemoryQueryBody(BaseModel):
    query: str = Field(min_length=1)
    workdir: str | None = None
    limit: int = Field(default=8, ge=1, le=50)
    env: dict[str, str] | None = None


def _confine_memory_workdir(runs_root: Path, requested: str | None) -> Path:
    """Keep a memory-query workdir under the tenant runs root.

    Absolute paths and ``..`` escapes are rejected before any directory is
    created, so a caller cannot mkdir an arbitrary host path.
    """

    base = (runs_root / "memory-query").resolve()
    if requested is None or not requested.strip():
        base.mkdir(parents=True, exist_ok=True)
        return base
    if looks_absolute(requested) or "\x00" in requested:
        raise V1HTTPError(
            400,
            code="invalid_workdir",
            message="workdir must be a relative path under the tenant workspace",
        )
    try:
        candidate = contained_path(base, requested)
    except PathEscapeError as exc:
        raise V1HTTPError(
            400,
            code="invalid_workdir",
            message="workdir escapes the tenant workspace",
        ) from exc
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def _tenant(
    ctx: ConsoleContext,
    principal: Any,
    request: Request,
    x_recertia_tenant: str | None,
) -> str:
    user = console_session(ctx.sessions, request)
    if x_recertia_tenant:
        if user is not None:
            if x_recertia_tenant not in user.tenants:
                raise V1HTTPError(
                    403, code="tenant_forbidden", message="tenant not in membership"
                )
            return x_recertia_tenant
        if x_recertia_tenant != principal.tenant_id:
            raise V1HTTPError(
                403, code="tenant_mismatch", message="api key tenant mismatch"
            )
        return x_recertia_tenant
    if user is not None:
        return user.active_tenant
    return str(principal.tenant_id)


def _safe_golden_dir(raw: str | None) -> Path | None:
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    root = (Path.cwd() / "evals" / "golden").resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise V1HTTPError(
            400,
            code="invalid_golden_dir",
            message="golden_dir must be under evals/golden",
        ) from exc
    return path


def _safe_golden_root(raw: str | None) -> Path:
    if not raw:
        return Path("evals/golden")
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    root = (Path.cwd() / "evals" / "golden").resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise V1HTTPError(
            400,
            code="invalid_golden_root",
            message="golden_root must be under evals/golden",
        ) from exc
    return path


def register_remaining_routes(app: FastAPI, ctx: ConsoleContext) -> None:
    require_runs = ctx.require_scope("runs", ctx.key_store)
    require_metrics = ctx.require_scope("metrics", ctx.key_store)

    @app.get("/v1/models")
    def list_models(principal=Depends(require_runs)) -> dict[str, Any]:
        """Server-side console model allowlist (OG-11). Never embed this in ``console/static/``."""

        return ctx.library_service.list_models(principal)

    @app.post("/v1/evals/runs")
    def evals_runs(
        body: EvalRunBody,
        request: Request,
        principal=Depends(require_metrics),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _tenant(ctx, principal, request, x_recertia_tenant)
        skills_root = ctx.tenant_skills_root(tenant_id)
        runs_root = ctx.tenant_runs_root(tenant_id)
        eval_db = runs_root / "evals.db"
        store = EvalStore(eval_db)
        try:
            report = ctx.eval_service.run_eval_suite(
                principal,
                task_class=body.task_class,
                golden_root=_safe_golden_root(body.golden_root),
                skills_root=skills_root,
                runs_root=runs_root / "eval-runs",
                eval_store=store,
                snapshot_id=body.snapshot or "eval-api",
                golden_dir=_safe_golden_dir(body.golden_dir),
            )
        except Forbidden as exc:
            raise V1HTTPError(403, code="forbidden", message=exc.message) from exc
        finally:
            store.close()
        return ctx.eval_service.suite_payload(report)

    @app.get("/v1/policy")
    def get_policy(principal=Depends(require_runs)) -> dict[str, Any]:
        return ctx.library_service.get_policy(principal)

    @app.post("/v1/policy/proposals")
    def propose_policy(
        body: PolicyProposalBody,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _tenant(ctx, principal, request, x_recertia_tenant)
        user = console_session(ctx.sessions, request)
        actor = user.user_id if user else ledger_actor(principal)
        ledger = HashChainLedger(ctx.tenant_runs_root(tenant_id) / "ledger.jsonl")
        rec = ctx.library_service.propose_policy(
            principal,
            store=ctx.proposals,
            tenant_id=tenant_id,
            policy_diff=body.policy_diff,
            rationale=body.rationale or "T2 policy proposal",
            actor=actor,
            ledger=ledger,
            created_by_job="policy-http",
        )
        return rec.to_dict()

    @app.get("/v1/reviews")
    def list_reviews(
        request: Request,
        principal=Depends(require_runs),
        status: str = "pending",
        limit: int = 50,
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _tenant(ctx, principal, request, x_recertia_tenant)
        return ctx.library_service.list_reviews(
            principal, store=ctx.proposals, tenant_id=tenant_id, status=status, limit=limit
        )

    @app.post("/v1/reviews/{decision_id}")
    def decide_review(
        decision_id: str,
        body: ReviewDecisionBody,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _tenant(ctx, principal, request, x_recertia_tenant)
        user = console_session(ctx.sessions, request)
        actor = user.user_id if user else ledger_actor(principal)
        ledger = HashChainLedger(ctx.tenant_runs_root(tenant_id) / "ledger.jsonl")
        try:
            updated = ctx.library_service.decide_review(
                principal,
                store=ctx.proposals,
                decision_id=decision_id,
                tenant_id=tenant_id,
                decision=body.decision,
                actor=actor,
                note=body.note,
                ledger=ledger,
                console_may_review=user.may("reviewer") if user is not None else None,
            )
        except ConfigError as exc:
            raise V1HTTPError(400, code="invalid_decision", message=exc.message) from exc
        except NotFound as exc:
            raise V1HTTPError(404, code="not_found", message="review not found") from exc
        except Forbidden as exc:
            raise V1HTTPError(403, code="forbidden", message=exc.message) from exc
        return updated.to_dict()

    @app.get("/v1/facts")
    def list_facts(
        request: Request,
        principal=Depends(require_runs),
        scope: str | None = None,
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _tenant(ctx, principal, request, x_recertia_tenant)
        store = FactStore(ctx.tenant_runs_root(tenant_id) / "facts")
        facts = ctx.library_service.list_facts(principal, store=store, scope=scope)
        return {"items": [f.model_dump(mode="json") for f in facts]}

    @app.get("/v1/cases")
    def list_cases(
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _tenant(ctx, principal, request, x_recertia_tenant)
        store = EpisodicStore(ctx.tenant_runs_root(tenant_id) / "episodic")
        return {"items": ctx.library_service.list_cases(principal, store=store)}

    @app.get("/v1/cases/{case_id}")
    def get_case(
        case_id: str,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _tenant(ctx, principal, request, x_recertia_tenant)
        store = EpisodicStore(ctx.tenant_runs_root(tenant_id) / "episodic")
        try:
            rec = ctx.library_service.get_case(principal, store=store, case_id=case_id)
        except NotFound as exc:
            raise V1HTTPError(404, code="not_found", message=exc.message) from exc
        return rec.model_dump(mode="json")

    @app.post("/v1/trajectories/import")
    def import_trajectory(
        body: dict[str, Any],
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _tenant(ctx, principal, request, x_recertia_tenant)
        try:
            return ctx.library_service.import_trajectory(
                principal,
                body=body,
                runs_root=ctx.root,
                tenant_id=tenant_id,
                actor=getattr(principal, "key_id", "api"),
            )
        except ConfigError as exc:
            raise V1HTTPError(400, code="import_rejected", message=exc.message) from exc

    @app.post("/v1/trajectories/distill")
    def distill_trajectory(
        body: DistillTrajectoryBody,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        from recertia.memory.procedural.store import SkillStore

        tenant_id = _tenant(ctx, principal, request, x_recertia_tenant)
        store = SkillStore(ctx.tenant_skills_root(tenant_id))
        try:
            return ctx.library_service.distill_trajectory(
                principal,
                store=store,
                trajectory=body.trajectory,
                actor=getattr(principal, "key_id", "api"),
                task_class=body.task_class,
            )
        except ConfigError as exc:
            raise V1HTTPError(400, code="distill_rejected", message=exc.message) from exc

    @app.get("/v1/affordances")
    def list_affordances(
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _tenant(ctx, principal, request, x_recertia_tenant)
        store = AffordanceStore(ctx.tenant_runs_root(tenant_id) / "affordances.json")
        return ctx.library_service.list_affordances(principal, store=store)

    @app.post("/v1/memory/query")
    def memory_query(
        body: MemoryQueryBody,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _tenant(ctx, principal, request, x_recertia_tenant)
        runs_root = ctx.tenant_runs_root(tenant_id)
        workdir = _confine_memory_workdir(runs_root, body.workdir)
        return ctx.library_service.query_memory(
            principal,
            query=body.query,
            skills_root=ctx.tenant_skills_root(tenant_id),
            facts_root=runs_root / "facts",
            episodic_root=runs_root / "episodic",
            index_path=runs_root / "skill_index.db",
            workdir=workdir,
            env_fingerprint=body.env,
            limit=body.limit,
            affordance_path=runs_root / "affordances.json",
            tenant_id=tenant_id,
            tenant_root=runs_root,
        )
