"""Liveness, readiness, and console static index."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from recertia.api.deps import RouteState


def register_meta_routes(app: FastAPI, rs: RouteState) -> None:
    root = rs.ctx.root

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> JSONResponse:
        from recertia.api import _readiness

        ok, body = _readiness(root)
        return JSONResponse(status_code=200 if ok else 503, content=body)

    console_dir = Path(__file__).resolve().parents[4] / "console" / "static"
    if console_dir.is_dir():
        app.mount("/console/assets", StaticFiles(directory=console_dir), name="console-assets")

        @app.get("/console")
        @app.get("/console/")
        def console_index() -> FileResponse:
            return FileResponse(console_dir / "index.html")
