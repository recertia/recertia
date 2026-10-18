"""HTTP adapters. Handlers map RecertiaError to HTTP; services own policy."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from recertia.api.console_auth import console_session
from recertia.api.deps import RouteState
from recertia.api.errors import V1HTTPError
from recertia.api.schemas import ProposalDecision, ReviewDecisionBody
from recertia.errors import ConfigError, Forbidden, NotFound
from recertia.ledger import HashChainLedger
from recertia.ports.identity import ledger_actor


def register_proposals_routes(app: FastAPI, rs: RouteState) -> None:
    ctx = rs.ctx
    require_runs = rs.require_runs
    _optional_console_user = rs.optional_console_user
    _resolve_tenant = rs.resolve_tenant
    _require_library_write = rs.require_library_write
    _public_user = rs.public_user

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
    @app.get("/v1/reviews")
    def list_reviews(
        request: Request,
        principal=Depends(require_runs),
        status: str = "pending",
        limit: int = 50,
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
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
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
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

