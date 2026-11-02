"""SQLite session and tenant repositories round-trip."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from recertia.persistence.sqlite.session import SqliteSessionRepo, SqliteTenantRepo
from recertia.ports.identity import TenantId


def test_sqlite_session_and_tenant_repos_roundtrip(tmp_path: Path) -> None:
    tenant = TenantId("demo")
    with closing(SqliteTenantRepo(tmp_path / "control.db")) as tenants:
        tenants.upsert(tenant, display_name="Demo", status="active")
        assert tenants.get(tenant) == {"tenant_id": "demo", "display_name": "Demo", "status": "active"}
        assert tenants.list_ids() == [tenant]
        tenants.grant(tenant, "ada", "operator")
        tenants.grant(tenant, "ada", "tenant_admin")
        assert tenants.role_for(tenant, "ada") == "tenant_admin"
        assert tenants.role_for(tenant, "missing") is None

    with closing(SqliteSessionRepo(tmp_path / "control.db")) as sessions:
        expires = datetime.now(timezone.utc) + timedelta(hours=1)
        sessions.put("sid", {"user": "ada"}, tenant=tenant, expires_at=expires)
        assert sessions.get("sid") == {"user": "ada"}
        sessions.delete("sid")
        assert sessions.get("sid") is None
