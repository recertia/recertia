from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from recertia.api.auth import ApiKeyStore
from recertia.solver.container import (
    LocalExecutionCapability,
    run_configured_command,
    run_in_container,
    run_with_backend,
)
from recertia.solver.sandbox import SandboxError
from recertia.telemetry import JsonlSpanExporter, Telemetry, render_dashboard


def test_container_execution_fails_closed_without_oci_runtime(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("recertia.solver.container.container_runtime", lambda: None)
    with pytest.raises(SandboxError, match="Docker or Podman required"):
        run_in_container("true", workdir=tmp_path)


def test_local_executor_requires_explicit_capability(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "local")
    with pytest.raises(SandboxError, match="explicit LocalExecutionCapability"):
        run_with_backend("true", workdir=tmp_path, backend="local")
    proc = run_configured_command("true", workdir=tmp_path)
    assert proc.returncode == 0
    assert LocalExecutionCapability().purpose == "test-or-local-development"


def test_api_keys_are_salted_scoped_durable_and_revocable(tmp_path: Path) -> None:
    db = tmp_path / "api-keys.sqlite"
    keys = ApiKeyStore(db)
    issued = keys.issue(tenant_id="tenant-a", scopes={"runs"}, actor="operator")

    principal = keys.authenticate(issued.secret)
    assert principal is not None
    assert principal.tenant_id == "tenant-a"
    assert principal.scopes == frozenset({"runs"})

    with closing(sqlite3.connect(db)) as conn, conn:
        stored = conn.execute(
            "SELECT key_hash, salt, hash_alg FROM api_keys"
        ).fetchone()
        audit = conn.execute("SELECT action FROM api_key_audit ORDER BY id").fetchall()
    assert issued.secret.encode() not in stored[0]
    assert stored[2] == "hmac-sha256"
    assert [row[0] for row in audit] == ["issued", "authenticated"]

    assert keys.revoke(issued.key_id, actor="operator")
    assert keys.authenticate(issued.secret) is None


def test_telemetry_is_tenant_scoped_and_jsonl_is_append_only(tmp_path: Path) -> None:
    telemetry = Telemetry()
    exporter = JsonlSpanExporter(tmp_path / "telemetry.jsonl")
    telemetry.add_exporter(exporter)
    telemetry.emit("run.started", tenant_id="tenant-a", run_id="run-a")
    telemetry.emit("run.started", tenant_id="tenant-b", run_id="run-b")

    dashboard = render_dashboard(telemetry, tenant_id="tenant-a")
    assert dashboard["panels"][0]["values"]["run.started"] == 1
    assert len((tmp_path / "telemetry.jsonl").read_text().splitlines()) == 2
    with pytest.raises(ValueError, match="require tenant_id"):
        telemetry.emit("run.finished", run_id="run-a")  # type: ignore[call-arg]
