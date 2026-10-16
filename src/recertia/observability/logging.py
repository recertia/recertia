"""One JSON log line per record, with request and tenant context."""

from __future__ import annotations

import json
import logging
import traceback
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from recertia.settings import Settings

request_id_var: ContextVar[str] = ContextVar("recertia_request_id", default="")
tenant_var: ContextVar[str | None] = ContextVar("recertia_tenant_id", default=None)
run_var: ContextVar[str | None] = ContextVar("recertia_run_id", default=None)
owner_var: ContextVar[str | None] = ContextVar("recertia_owner_id", default=None)


def bind_log_context(
    *,
    request_id: str,
    tenant_id: str | None = None,
    run_id: str | None = None,
    owner_id: str | None = None,
) -> tuple[object, object, object, object]:
    return (
        request_id_var.set(request_id),
        tenant_var.set(tenant_id),
        run_var.set(run_id),
        owner_var.set(owner_id),
    )


def reset_log_context(tokens: tuple[object, object, object, object]) -> None:
    request_id_var.reset(tokens[0])  # type: ignore[arg-type]
    tenant_var.reset(tokens[1])  # type: ignore[arg-type]
    run_var.reset(tokens[2])  # type: ignore[arg-type]
    owner_var.reset(tokens[3])  # type: ignore[arg-type]


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
            "tenant_id": tenant_var.get(),
            "run_id": run_var.get(),
            "owner_id": owner_var.get(),
        }
        if record.exc_info:
            payload["exc_info"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(settings: Settings) -> None:
    root = logging.getLogger()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root.setLevel(level)
    handler = logging.StreamHandler()
    if settings.log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root.handlers = [handler]
