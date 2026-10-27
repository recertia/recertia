def run_events(
    run_id: str,
    request: Request,
    principal=Depends(require_runs),
    after: str | None = None,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
) -> StreamingResponse:
    """SSE follow. REC-056g: honour Last-Event-ID header."""
    resume_after = last_event_id or after
    # full file follows in next update
