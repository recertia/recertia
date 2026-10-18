"""HTTP adapters. Handlers map RecertiaError to HTTP; services own policy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, Request

from recertia.api.console_auth import console_session
from recertia.api.deps import RouteState
from recertia.api.errors import V1HTTPError
from recertia.api.schemas import EvalRunBody
from recertia.errors import Forbidden
from recertia.evals.store import EvalStore


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
            400, code="invalid_golden_dir", message="golden_dir must be under evals/golden"
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
            400, code="invalid_golden_root", message="golden_root must be under evals/golden"
        ) from exc
    return path


def register_evals_routes(app: FastAPI, rs: RouteState) -> None:
    ctx = rs.ctx
    require_metrics = rs.require_metrics
    _optional_console_user = rs.optional_console_user
    _resolve_tenant = rs.resolve_tenant
    _require_library_write = rs.require_library_write
    _public_user = rs.public_user

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

