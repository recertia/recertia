"""RuntimeLayout path rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from recertia.persistence.layout import RuntimeLayout
from recertia.ports.identity import TenantId


def test_tenant_accessor_rejects_path_escape(tmp_path: Path) -> None:
    layout = RuntimeLayout(tmp_path)
    with pytest.raises(ValueError):
        layout.tenant("../other")


def test_run_events_path_is_tenant_scoped(tmp_path: Path) -> None:
    layout = RuntimeLayout(tmp_path)
    events = layout.tenant(TenantId("acme")).run_events_path("run-1")
    assert events == tmp_path / "runs" / "acme" / "run_events" / "run-1.jsonl"
    assert layout.local_runs().ledger_path == tmp_path / "ledger.jsonl"
    assert layout.control_db == tmp_path / "control.db"
