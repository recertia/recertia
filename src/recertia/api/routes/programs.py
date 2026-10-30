"""HTTP adapters. Handlers map RecertiaError to HTTP; services own policy."""

from __future__ import annotations

import re
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request

from recertia.api.deps import RouteState
from recertia.api.schemas import (
    ProgramAccept,
    ProgramCreate,
    ProgramFromPack,
    RecordTipBody,
    RepoBindingBody,
    SeedWorkdirBody,
    StepPatch,
    StepRunBody,
    StepSkipBody,
)
from recertia.errors import ConfigError, RecertiaError
from recertia.persistence.root_lock import RootBusy
from recertia.runtime.errors import PreconditionRequired, ProgramConflict

# A program version as a bare or quoted integer: 3 or "3". Weak tags, lists and * are refused.
_IF_MATCH_RE = re.compile(r'(?:"([0-9]{1,18})"|([0-9]{1,18}))')


def _map_program_error(exc: RecertiaError) -> HTTPException:
    if isinstance(exc, (RootBusy, ProgramConflict, PreconditionRequired)):
        # Re-raise for the app-level RecertiaError handler, which sends the error's own code
        # (root_busy, program_conflict, precondition_required). A plain HTTPException would
        # say unavailable, conflict or http_error.
        raise exc
    detail: Any = exc.details.get("http_detail", exc.message) if exc.details else exc.message
    return HTTPException(status_code=exc.http_status, detail=detail)


def _parse_if_match(raw: str | None) -> int | None:
    """``If-Match`` as a program version; ``None`` when the header is absent or blank."""

    if raw is None or not raw.strip():
        return None
    m = _IF_MATCH_RE.fullmatch(raw.strip())
    if m is None:
        raise ConfigError('If-Match must be a program version, for example "3"')
    return int(m.group(1) or m.group(2))


def register_programs_routes(app: FastAPI, rs: RouteState) -> None:
    ctx = rs.ctx
    require_runs = rs.require_runs
    _optional_console_user = rs.optional_console_user
    _resolve_tenant = rs.resolve_tenant
    _require_library_write = rs.require_library_write
    _public_user = rs.public_user

    @app.post("/v1/programs/from-pack")
    def program_from_pack(
        body: ProgramFromPack,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        """Accept a Compose decomposition/pack draft into a durable MigrationProgram."""

        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        try:
            return ctx.program_service.create_from_pack(
                principal, tenant=tenant_id, runtime=ctx, body=body
            )
        except RecertiaError as exc:
            raise _map_program_error(exc) from exc
    @app.post("/v1/programs")
    def create_program(
        request: Request,
        body: ProgramCreate,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        try:
            return ctx.program_service.create(
                principal, tenant=tenant_id, runtime=ctx, body=body
            )
        except RecertiaError as exc:
            raise _map_program_error(exc) from exc
    @app.get("/v1/programs")
    def list_programs(
        request: Request,
        status: str | None = None,
        limit: int = Query(50, ge=1, le=100),
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        try:
            return ctx.program_service.list(
                principal, tenant=tenant_id, runtime=ctx, status=status, limit=limit
            )
        except RecertiaError as exc:
            raise _map_program_error(exc) from exc
    @app.get("/v1/programs/{program_id}")
    def get_program(
        program_id: str,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        try:
            return ctx.program_service.get(
                principal, tenant=tenant_id, runtime=ctx, program_id=program_id
            )
        except RecertiaError as exc:
            raise _map_program_error(exc) from exc
    @app.put("/v1/programs/{program_id}")
    def replace_program(
        program_id: str,
        body: ProgramCreate,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
        if_match: str | None = Header(default=None, alias="If-Match"),
    ) -> dict[str, Any]:
        """Replace a draft program. ``If-Match`` must carry the version last read."""

        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        try:
            return ctx.program_service.replace(
                principal,
                tenant=tenant_id,
                runtime=ctx,
                program_id=program_id,
                body=body,
                if_match=_parse_if_match(if_match),
            )
        except RecertiaError as exc:
            raise _map_program_error(exc) from exc
    @app.post("/v1/programs/{program_id}/accept")
    def accept_program(
        program_id: str,
        body: ProgramAccept,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        try:
            return ctx.program_service.accept(
                principal, tenant=tenant_id, runtime=ctx, program_id=program_id, body=body
            )
        except RecertiaError as exc:
            raise _map_program_error(exc) from exc
    @app.post("/v1/programs/{program_id}/abandon")
    def abandon_program(
        program_id: str,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        try:
            return ctx.program_service.abandon(
                principal, tenant=tenant_id, runtime=ctx, program_id=program_id
            )
        except RecertiaError as exc:
            raise _map_program_error(exc) from exc
    @app.patch("/v1/programs/{program_id}/steps/{step_id}")
    def patch_step(
        program_id: str,
        step_id: str,
        body: StepPatch,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        try:
            return ctx.program_service.patch_step(
                principal,
                tenant=tenant_id,
                runtime=ctx,
                program_id=program_id,
                step_id=step_id,
                body=body,
            )
        except RecertiaError as exc:
            raise _map_program_error(exc) from exc
    @app.post("/v1/programs/{program_id}/steps/{step_id}/preview")
    def preview_step(
        program_id: str,
        step_id: str,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        try:
            return ctx.program_service.preview_step(
                principal,
                tenant=tenant_id,
                runtime=ctx,
                program_id=program_id,
                step_id=step_id,
            )
        except RecertiaError as exc:
            raise _map_program_error(exc) from exc
    @app.post("/v1/programs/{program_id}/steps/{step_id}/run")
    def run_step(
        program_id: str,
        step_id: str,
        body: StepRunBody,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        """GP0: plan_only preview envelope, or bind an existing run_id after POST /v1/runs."""

        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        try:
            return ctx.program_service.run_step(
                principal,
                tenant=tenant_id,
                runtime=ctx,
                program_id=program_id,
                step_id=step_id,
                body=body,
            )
        except RecertiaError as exc:
            raise _map_program_error(exc) from exc
    @app.post("/v1/programs/{program_id}/steps/{step_id}/skip")
    def skip_step(
        program_id: str,
        step_id: str,
        body: StepSkipBody,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        try:
            return ctx.program_service.skip_step(
                principal,
                tenant=tenant_id,
                runtime=ctx,
                program_id=program_id,
                step_id=step_id,
                body=body,
            )
        except RecertiaError as exc:
            raise _map_program_error(exc) from exc
    @app.post("/v1/programs/{program_id}/repo-binding")
    def set_repo_binding(
        program_id: str,
        body: RepoBindingBody,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        try:
            return ctx.program_service.bind_repo(
                principal, tenant=tenant_id, runtime=ctx, program_id=program_id, body=body
            )
        except RecertiaError as exc:
            raise _map_program_error(exc) from exc
    @app.post("/v1/programs/{program_id}/steps/{step_id}/record-tip")
    def record_step_tip(
        program_id: str,
        step_id: str,
        body: RecordTipBody,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        try:
            return ctx.program_service.record_tip(
                principal,
                tenant=tenant_id,
                runtime=ctx,
                program_id=program_id,
                step_id=step_id,
                body=body,
            )
        except RecertiaError as exc:
            raise _map_program_error(exc) from exc
    @app.post("/v1/programs/{program_id}/steps/{step_id}/seed-workdir")
    def seed_step_workdir(
        program_id: str,
        step_id: str,
        body: SeedWorkdirBody,
        request: Request,
        principal=Depends(require_runs),
        x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
    ) -> dict[str, Any]:
        """Checkout predecessor tip into a fresh canonical run workdir (no shared mount)."""

        tenant_id = _resolve_tenant(principal, request, x_recertia_tenant)
        try:
            return ctx.program_service.seed_workdir(
                principal,
                tenant=tenant_id,
                runtime=ctx,
                program_id=program_id,
                step_id=step_id,
                body=body,
            )
        except RecertiaError as exc:
            raise _map_program_error(exc) from exc

