"""HTTP adapters. Handlers map RecertiaError to HTTP; services own policy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, Request

from recertia.api.console_auth import console_session
from recertia.api.deps import RouteState
from recertia.api.errors import V1HTTPError
from recertia.api.schemas import MemoryQueryBody, PolicyProposalBody
from recertia.errors import NotFound
from recertia.ledger import HashChainLedger
from recertia.memory.affordance import AffordanceStore
from recertia.memory.episodic import EpisodicStore
from recertia.memory.semantic import FactStore
from recertia.paths import PathEscapeError, contained_path, looks_absolute
from recertia.ports.identity import ledger_actor


def _tenant(ctx, principal, request, x_recertia_tenant):
    user = console_session(ctx.sessions, request)
    if x_recertia_tenant:
        if user is not None:
            if x_recertia_tenant not in user.tenants:
                raise V1HTTPError(403, code="tenant_forbidden", message="tenant not in membership")
            return x_recertia_tenant
        if x_recertia_tenant != principal.tenant_id:
            raise V1HTTPError(403, code="tenant_mismatch", message="api key tenant mismatch")
        return x_recertia_tenant
    if user is not None:
        return user.active_tenant
    return str(principal.tenant_id)


def _confine_memory_workdir(runs_root: Path, requested: str | None) -> Path:
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


def register_library_routes(app: FastAPI, rs: RouteState) -> None:
    ctx = rs.ctx
    require_runs = rs.require_runs
    _optional_console_user = rs.optional_console_user
    _resolve_tenant = rs.resolve_tenant
    _require_library_write = rs.require_library_write
    _public_user = rs.public_user

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
    @app.get("/v1/models")
    def list_models(principal=Depends(require_runs)) -> dict[str, Any]:
        """Server-side console model allowlist (OG-11). Never embed this in ``console/static/``."""

        return ctx.library_service.list_models(principal)
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

