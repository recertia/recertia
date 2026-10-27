"""``/v1/runs`` create, fetch, list, resume, cancel, transcript, trajectory, events."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

# ... remainder of file to be added in subsequent updates
# Last-Event-ID support is present in the full version
