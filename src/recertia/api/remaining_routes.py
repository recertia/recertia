"""Remaining HTTP surface (RW-SUR): evals, policy, reviews alias, memory planes."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Request
from pydantic import BaseModel, Field

from recertia.api.console_routes import ConsoleContext
from recertia.api.errors import V1HTTPError
from recertia.evals.golden import run_eval_suite
from recertia.evals.store import EvalStore
from recertia.ledger import HashChainLedger
from recertia.memory.affordance import AffordanceStore
from recertia.memory.episodic import EpisodicStore
from recertia.memory.query import federated_query
from recertia.memory.semantic import FactStore
from recertia.policy_load import load_policy
from recertia.proposals.store import ProposalRecord
from recertia.trajectory.import_store import ImportRejected, ingest_trajectory


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


class MemoryQueryBody(BaseModel):
    query: str = Field(min_length=1)
    workdir: str | None = None
    limit: int = Field(default=8, ge=1, le=50)
    env: dict[str, str] | None = None


def _tenant(
    ctx: ConsoleContext,
    principal: Any,
    request: Request,
    x_recertia_tenant: str | None,
) -> str:
    token = request.headers.get("X-Recertia-Session") or request.cookies.get(
        "recertia_session"
    )
    user = ctx.sessions.parse(token)
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

        del principal
        from recertia.solver.model_allowlist import load_model_allowlist

        return {"models": list(load_model_allowlist())}

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
            report = run_eval_suite(
                task_class=body.task_class,
                golden_root=_safe_golden_root(body.golden_root),
                skills_root=skills_root,
                runs_root=runs_root / "eval-runs",
                eval_store=store,
                snapshot_id=body.snapshot or "eval-api",
                golden_dir=_safe_golden_dir(body.golden_dir),
            )
        finally:
            store.close()
        return {
            "all_passed": report.all_passed,
            "results": [
                {
                    "skill_id": r.skill_id,
                    "passed": r.passed,
                    "terminal": r.terminal,
                    "run_id": r.run_id,
                    "detail": r.detail,
                }
                for r in report.results
            ],
        }

    @app.get("/v1/policy")
    def get_policy(principal=Depends(require_runs)) -> dict[str, Any]:
        del principal
        policy = load_policy()
        return policy.model_dump(mode="json")

    @app.post("/v1/policy/proposals")
    def propose_policy(
        body: PolicyProposalBody,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _tenant(ctx, principal, request, x_recertia_tenant)
        token = request.headers.get("X-Recertia-Session") or request.cookies.get(
            "recertia_session"
        )
        user = ctx.sessions.parse(token)
        actor = user.user_id if user else principal.key_id
        rec = ProposalRecord(
            proposal_id=uuid4().hex[:12],
            kind="policy",
            skill_id="policy",
            version=0,
            rationale=body.rationale or "T2 policy proposal",
            payload={"policy_diff": body.policy_diff, "tier": "T2", "applied": False},
            tenant_id=tenant_id,
            created_by_job="policy-http",
        )
        ctx.proposals.add(rec)
        ledger = HashChainLedger(ctx.tenant_runs_root(tenant_id) / "ledger.jsonl")
        ledger.append(
            actor=actor,
            action="policy_change",
            target=f"proposal:{rec.proposal_id}",
            evidence={"kind": "policy_proposal", "applied": False},
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
        items = ctx.proposals.list(tenant_id=tenant_id, status=status, limit=limit)
        return {
            "alias_of": "proposals",
            "items": [p.to_dict() for p in items],
        }

    @app.post("/v1/reviews/{decision_id}")
    def decide_review(
        decision_id: str,
        body: ReviewDecisionBody,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _tenant(ctx, principal, request, x_recertia_tenant)
        if body.decision not in {"approve", "reject", "request_changes"}:
            raise V1HTTPError(
                400, code="invalid_decision", message="approve|reject|request_changes"
            )
        token = request.headers.get("X-Recertia-Session") or request.cookies.get(
            "recertia_session"
        )
        user = ctx.sessions.parse(token)
        actor = user.user_id if user else principal.key_id
        rec = ctx.proposals.get(decision_id, tenant_id=tenant_id)
        if rec is None:
            raise V1HTTPError(404, code="not_found", message="review not found")
        try:
            updated = ctx.proposals.decide(
                decision_id,
                tenant_id=tenant_id,
                decision=body.decision,
                actor=actor,
                note=body.note,
            )
        except ValueError as exc:
            raise V1HTTPError(400, code="invalid_decision", message=str(exc)) from exc
        ledger = HashChainLedger(ctx.tenant_runs_root(tenant_id) / "ledger.jsonl")
        ledger.append(
            actor=actor,
            action="policy_change",
            target=f"review:{decision_id}",
            evidence={
                "kind": "review_decision",
                "decision": body.decision,
                "lifecycle_approved": False,
            },
        )
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
        facts = store.list_facts(scope=scope)
        return {"items": [f.model_dump(mode="json") for f in facts]}

    @app.get("/v1/cases")
    def list_cases(
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _tenant(ctx, principal, request, x_recertia_tenant)
        store = EpisodicStore(ctx.tenant_runs_root(tenant_id) / "episodic")
        return {"items": store.list_index()}

    @app.get("/v1/cases/{case_id}")
    def get_case(
        case_id: str,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _tenant(ctx, principal, request, x_recertia_tenant)
        store = EpisodicStore(ctx.tenant_runs_root(tenant_id) / "episodic")
        rec = store.get_by_case_id(case_id)
        if rec is None:
            raise V1HTTPError(404, code="not_found", message="case not found")
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
            result = ingest_trajectory(
                body,
                runs_root=ctx.root,
                tenant_id=tenant_id,
                actor=principal.key_id,
            )
        except (ImportRejected, ValueError) as exc:
            raise V1HTTPError(400, code="import_rejected", message=str(exc)) from exc
        return {
            "import_id": result.import_id,
            "case_id": result.case_id,
            "proposal_id": result.proposal_id,
            "reexecutable": result.reexecutable,
            "promoted": result.promoted,
        }

    @app.get("/v1/affordances")
    def list_affordances(
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _tenant(ctx, principal, request, x_recertia_tenant)
        store = AffordanceStore(ctx.tenant_runs_root(tenant_id) / "affordances.json")
        return {
            "tools": [
                {
                    "tool": t.tool,
                    "invocations": t.invocations,
                    "failures": t.failures,
                    "failure_rate": t.failure_rate,
                }
                for t in store.tools.values()
            ],
            "resources": [
                {
                    "kind": r.kind,
                    "id": r.id,
                    "conflicts": r.conflicts,
                    "timeouts": r.timeouts,
                }
                for r in store.resources.values()
            ],
        }

    @app.post("/v1/memory/query")
    def memory_query(
        body: MemoryQueryBody,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _tenant(ctx, principal, request, x_recertia_tenant)
        runs_root = ctx.tenant_runs_root(tenant_id)
        workdir = Path(body.workdir) if body.workdir else runs_root / "memory-query"
        workdir.mkdir(parents=True, exist_ok=True)
        return federated_query(
            body.query,
            skills_root=ctx.tenant_skills_root(tenant_id),
            facts_root=runs_root / "facts",
            episodic_root=runs_root / "episodic",
            index_path=runs_root / "skill_index.db",
            workdir=workdir,
            env_fingerprint=body.env,
            limit=body.limit,
            affordance_path=runs_root / "affordances.json",
        )
