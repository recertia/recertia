"""Postgres backend stays optional. The suite does not need a server."""

from __future__ import annotations

import os

import pytest

from recertia.store.backend import PostgresBackend


@pytest.mark.skipif(not os.environ.get("RECERTIA_TEST_PG_DSN"), reason="RECERTIA_TEST_PG_DSN unset")
def test_postgres_backend_connects_when_dsn_is_set() -> None:
    backend = PostgresBackend(dsn=os.environ["RECERTIA_TEST_PG_DSN"])
    conn = backend.connect()
    assert conn is not None
    backend.close()
