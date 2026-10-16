"""In-process Prometheus text registry. Labels stay low-cardinality."""

from __future__ import annotations

from collections import defaultdict
from threading import Lock

CATALOGUE = (
    "recertia_http_requests",
    "recertia_http_duration_seconds",
    "recertia_runs_started",
    "recertia_runs_finished",
    "recertia_queue_depth",
    "recertia_inflight_runs",
    "recertia_worker_up",
    "recertia_ledger_appends",
    "recertia_ledger_verify_seconds",
    "recertia_outbox_pending",
    "recertia_outbox_dead",
    "recertia_model_calls",
    "recertia_model_duration_seconds",
    "recertia_model_tokens",
    "recertia_model_cost_usd",
    "recertia_model_cost_usd_late",
    "recertia_circuit_state",
    "recertia_quota_reserved_tokens",
    "recertia_backup_last_success_unixtime",
    "recertia_disk_free_bytes",
    "recertia_sse_connections",
    "recertia_tenant_info",
)


class Metrics:
    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}

    def inc(self, name: str, *, value: float = 1, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._counters[key] += value

    def observe(self, name: str, value: float, **labels: str) -> None:
        self.inc(name, value=value, **labels)

    def set_gauge(self, name: str, value: float, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._gauges[key] = value

    def render_prometheus(self) -> bytes:
        lines: list[str] = []
        with self._lock:
            items = list(self._counters.items()) + [(k, v) for k, v in self._gauges.items()]
        for (name, labels), value in sorted(items, key=lambda item: item[0][0]):
            if labels:
                rendered = ",".join(f'{key}="{val}"' for key, val in labels)
                lines.append(f"{name}{{{rendered}}} {value}")
            else:
                lines.append(f"{name} {value}")
        text = ("\n".join(lines) + "\n") if lines else ""
        return text.encode("utf-8")


metrics = Metrics()
