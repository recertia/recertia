"""SQLite session and tenant repositories round-trip."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from recertia.persistence.sqlite.session import SqliteSessionRepo, SqliteTenantRepo
from recertia.ports.identity import TenantId


def test_sqlite_session_and_tenant_repos_roundtrip(tmp_path: Path) -> None:
    tenant = TenantId("demo")
    tenants = SqliteTenantRepo(tmp_path / "control.db")
    tenants.upsert(tenant, display_name="Demo", status="active")
    assert tenants.get(tenant) == {"tenant_id": "demo", "display_name": "Demo", "status": "active"}
    assert tenants.list_ids() == [tenant]

    sessions = SqliteSessionRepo(tmp_path / "control.db")
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    sessions.put("sid", {"user": "ada"}, tenant=tenant, expires_at=expires)
    assert sessions.get("sid") == {"user": "ada"}
    sessions.delete("sid")
    assert sessions.get("sid") is None
