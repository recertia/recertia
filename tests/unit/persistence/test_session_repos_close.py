"""The SQLite session and tenant repositories close their connection."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from recertia.persistence.sqlite.session import SqliteSessionRepo, SqliteTenantRepo
from recertia.ports.identity import TenantId


def test_session_repo_close_closes_the_connection(tmp_path: Path) -> None:
    with closing(SqliteSessionRepo(tmp_path / "control.db")) as sessions:
        assert sessions.get("sid") is None

    with pytest.raises(sqlite3.ProgrammingError):
        sessions.get("sid")


def test_tenant_repo_close_closes_the_connection(tmp_path: Path) -> None:
    with closing(SqliteTenantRepo(tmp_path / "control.db")) as tenants:
        assert tenants.list_ids() == []

    with pytest.raises(sqlite3.ProgrammingError):
        tenants.get(TenantId("demo"))
