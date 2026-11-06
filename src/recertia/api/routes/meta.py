"""Liveness, readiness, and console static index."""

from __future__ import annotations

from importlib.resources import as_file, files
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from recertia.api.deps import RouteState


def _packaged_console_static() -> Path | None:
    """The packaged ``console/static`` directory, or ``None`` if it is missing.

    Resolved through ``importlib.resources.files("recertia")``. Wheels install package
    data as real files under site-packages, so ``as_file`` returns a lasting path.
    """

    resource = files("recertia").joinpath("console", "static")
    if not resource.is_dir():
        return None
    with as_file(resource) as path:
        resolved = Path(path)
    return resolved if resolved.is_dir() else None


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

    console_dir = _packaged_console_static()
    if console_dir is not None:
        app.mount("/console/assets", StaticFiles(directory=console_dir), name="console-assets")

        @app.get("/console")
        @app.get("/console/")
        def console_index() -> FileResponse:
            return FileResponse(console_dir / "index.html")
