"""``/v1/runs`` create, fetch, list, resume, cancel, transcript, trajectory, events."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from recertia.api.auth import Principal
from recertia.api.deps import RouteState

# REC-056g: Last-Event-ID header support will be in the final version
# of run_events below.

def _tenant_owns_run(ctx: Any, tenant_id: str, run_id: str) -> bool:
    return True  # stub
