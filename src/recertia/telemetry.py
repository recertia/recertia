"""OpenTelemetry-shaped spans and required operational events (M9 hardening).

Uses the OpenTelemetry API when installed; otherwise an in-process recorder so CI and local
runs stay dependency-light while still asserting the required event surface.
"""

from __future__ import annotations

import contextvars
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

REQUIRED_EVENTS = frozenset(
    {
        "run.started",
        "run.finished",
        "node.started",
        "node.finished",
        "tool.invoked",
        "judge.context.opened",
        "merge.audited",
        "policy.changed",
        "scope.promoted",
    }
)


@dataclass
class SpanEvent:
    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class SpanRecord:
    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[SpanEvent] = field(default_factory=list)
    status: str = "ok"
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None


class Telemetry:
    """Process-local span/event recorder (OTel-compatible surface)."""

    def __init__(self) -> None:
        self.spans: list[SpanRecord] = []
        self.events: list[SpanEvent] = []
        self._otel = None
        self._exporters: list[Any] = []
        try:
            from opentelemetry import trace  # type: ignore

            self._otel = trace.get_tracer("recertia")
        except Exception:  # noqa: BLE001 — optional dependency
            self._otel = None

    def add_exporter(self, exporter: Any) -> None:
        self._exporters.append(exporter)

    def emit(
        self, name: str, *, tenant_id: str | None = None, run_id: str | None = None, **attributes: Any
    ) -> SpanEvent:
        if not tenant_id:
            raise ValueError("telemetry events require tenant_id")
        if name.startswith(("run.", "node.", "tool.", "judge.")) and not run_id:
            raise ValueError(f"telemetry event {name!r} requires run_id")
        attributes = {"tenant_id": tenant_id, **({"run_id": run_id} if run_id else {}), **attributes}
        event = SpanEvent(name=name, attributes=attributes)
        self.events.append(event)
        for exporter in self._exporters:
            exporter.export_event(event)
        return event

    @contextmanager
    def span(
        self, name: str, *, tenant_id: str | None = None, run_id: str | None = None, **attributes: Any
    ) -> Iterator[SpanRecord]:
        if not tenant_id:
            raise ValueError("telemetry spans require tenant_id")
        attributes = {"tenant_id": tenant_id, **({"run_id": run_id} if run_id else {}), **attributes}
        record = SpanRecord(name=name, attributes=attributes)
        self.spans.append(record)
        otel_cm = None
        if self._otel is not None:
            otel_cm = self._otel.start_as_current_span(name)
            otel_cm.__enter__()
        try:
            yield record
        except Exception:
            record.status = "error"
            raise
        finally:
            record.ended_at = datetime.now(timezone.utc)
            for exporter in self._exporters:
                exporter.export_span(record)
            if otel_cm is not None:
                otel_cm.__exit__(None, None, None)

    def missing_required(self, *, tenant_id: str | None = None) -> list[str]:
        seen = {
            e.name
            for e in self.events
            if tenant_id is None or e.attributes.get("tenant_id") == tenant_id
        }
        return sorted(REQUIRED_EVENTS - seen)


class JsonlSpanExporter:
    """Append spans/events as JSONL — local OTLP stand-in for CI and ops."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def export_event(self, event: SpanEvent) -> None:
        self._write({"type": "event", **_event_dict(event)})

    def export_span(self, span: SpanRecord) -> None:
        self._write({"type": "span", **_span_dict(span)})

    def _write(self, payload: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")


def _event_dict(event: SpanEvent) -> dict[str, Any]:
    return {"name": event.name, "attributes": event.attributes, "at": event.at.isoformat()}


def _span_dict(span: SpanRecord) -> dict[str, Any]:
    return {
        "name": span.name,
        "attributes": span.attributes,
        "status": span.status,
        "started_at": span.started_at.isoformat(),
        "ended_at": span.ended_at.isoformat() if span.ended_at else None,
        "events": [_event_dict(e) for e in span.events],
    }


def render_dashboard(
    tel: Telemetry,
    *,
    tenant_id: str,
    metric_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Operational dashboard payload (Grafana-compatible-ish summary JSON)."""

    by_name: dict[str, int] = {}
    for e in tel.events:
        if e.attributes.get("tenant_id") != tenant_id:
            continue
        by_name[e.name] = by_name.get(e.name, 0) + 1
    panels: list[dict[str, Any]] = [
        {
            "id": "required_events",
            "type": "stat",
            "targets": sorted(REQUIRED_EVENTS),
            "values": {name: by_name.get(name, 0) for name in sorted(REQUIRED_EVENTS)},
        },
        {
            "id": "spans",
            "type": "table",
            "rows": [
                {
                    "name": s.name,
                    "status": s.status,
                    "started_at": s.started_at.isoformat(),
                }
                for s in tel.spans
                if s.attributes.get("tenant_id") == tenant_id
            ],
        },
    ]
    if metric_summary is not None:
        panels.append({"id": "eval_metrics", "type": "stat", "values": metric_summary})
    return {
        "title": "Recertia ops",
        "panels": panels,
        "missing_required": tel.missing_required(tenant_id=tenant_id),
    }


def write_dashboard(tel: Telemetry, path: Path | str, *, tenant_id: str) -> Path:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(render_dashboard(tel, tenant_id=tenant_id), indent=2) + "\n", encoding="utf-8")
    return dest


_GLOBAL = Telemetry()
_ACTIVE_TENANT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "recertia_telemetry_tenant", default=None
)
_ACTIVE_RUN: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "recertia_telemetry_run", default=None
)


def get_telemetry() -> Telemetry:
    return _GLOBAL


@contextmanager
def telemetry_run(*, tenant_id: str, run_id: str) -> Iterator[None]:
    """Bind tenant/run for nested tool and model emits."""

    tenant_tok = _ACTIVE_TENANT.set(tenant_id)
    run_tok = _ACTIVE_RUN.set(run_id)
    try:
        yield
    finally:
        _ACTIVE_RUN.reset(run_tok)
        _ACTIVE_TENANT.reset(tenant_tok)


def emit_in_run(name: str, **attributes: Any) -> SpanEvent | None:
    """Emit if a run context is bound; otherwise no-op (unit tests, offline tools)."""

    tenant_id = _ACTIVE_TENANT.get()
    if not tenant_id:
        return None
    return get_telemetry().emit(name, tenant_id=tenant_id, run_id=_ACTIVE_RUN.get(), **attributes)


def reset_telemetry(*, admin_actor: str, tenant_id: str) -> Telemetry:
    """Test/maintenance-only reset, recorded before the recorder is replaced."""

    if not admin_actor or not tenant_id:
        raise PermissionError("telemetry reset requires an authenticated admin actor and tenant")
    global _GLOBAL
    _GLOBAL.emit(
        "telemetry.reset",
        tenant_id=tenant_id,
        actor=admin_actor,
        reason="explicit administrative reset",
    )
    _GLOBAL = Telemetry()
    return _GLOBAL
