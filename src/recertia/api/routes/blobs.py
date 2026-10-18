"""Blob put/get. Size cap stays in the HTTP adapter (413)."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, HTTPException

from recertia.api.auth import Principal
from recertia.api.deps import RouteState
from recertia.errors import NotFound as RecertiaNotFound
from recertia.store.blobs import FilesystemBlobStore


def register_blobs_routes(app: FastAPI, rs: RouteState) -> None:
    ctx = rs.ctx
    require_blobs = ctx.require_scope("blobs", ctx.key_store)

    def _store(tenant_id: str) -> FilesystemBlobStore:
        from recertia.api import _tenant_blob_root

        blobs_by_tenant: dict[str, FilesystemBlobStore] = app.state.blobs_by_tenant
        return blobs_by_tenant.setdefault(
            tenant_id, FilesystemBlobStore(_tenant_blob_root(ctx.root, tenant_id))
        )

    @app.post("/v1/blobs")
    def put_blob(
        payload: dict[str, Any],
        principal: Principal = Depends(require_blobs),
    ) -> dict[str, str]:
        from recertia.api import _max_blob_bytes

        blobs = _store(principal.tenant_id)
        data = str(payload.get("data", "")).encode()
        if len(data) > _max_blob_bytes():
            raise HTTPException(
                status_code=413,
                detail=f"blob exceeds max size of {_max_blob_bytes()} bytes",
            )
        try:
            return ctx.library_service.put_blob(
                principal,
                store=blobs,
                data=data,
                content_type=str(payload.get("content_type", "text/plain")),
            )
        except RecertiaNotFound as exc:
            raise HTTPException(status_code=404, detail=exc.message) from exc

    @app.get("/v1/blobs/{digest}")
    def get_blob(
        digest: str, principal: Principal = Depends(require_blobs)
    ) -> dict[str, Any]:
        blobs = _store(principal.tenant_id)
        try:
            return ctx.library_service.get_blob(principal, store=blobs, digest=digest)
        except RecertiaNotFound as exc:
            raise HTTPException(status_code=404, detail=exc.message) from exc
