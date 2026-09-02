#!/usr/bin/env python3
"""Postgres soak helper: apply migrations against DATABASE_URL (roadmap P0-5 / P2-1).

Requires ``DATABASE_URL=postgres://...`` (or ``postgresql://...``). The compose file
``docker-compose.soak.yml`` provisions Postgres 16 with pgvector
(``pgvector/pgvector:pg16``) for this script. Plain ``postgres:16`` cannot
``CREATE EXTENSION vector``.

Golden suite remains a separate CI/ops step; this script focuses on durable store soak.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-migrations",
        action="store_true",
        help="Only check DSN / dialect without applying SQL.",
    )
    parser.add_argument(
        "--recertia-root",
        type=Path,
        default=Path(".recertia"),
        help="Optional .recertia/ snapshot to note alongside the Postgres soak.",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn.startswith(("postgres://", "postgresql://")):
        print(
            "DATABASE_URL must be a postgres DSN for soak "
            "(code reads DATABASE_URL; RECERTIA_STORE_BACKEND is docs-only).",
            file=sys.stderr,
        )
        return 2

    from recertia.store.backend import PostgresBackend, postgres_dialect_mentions_pgvector

    backend = PostgresBackend(dsn=dsn)
    try:
        print(f"dialect={backend.dialect}")
        print(f"pgvector_mentioned={postgres_dialect_mentions_pgvector()}")
        if not args.skip_migrations:
            newly = backend.apply_migrations()
            print(f"migrations_applied={newly}")
        tables = backend.table_names()
        print(f"tables={sorted(tables)}")
        conn = backend._conn or backend.connect()
        with conn.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute("SELECT 1")
            assert cur.fetchone()[0] == 1
        print("soak=ok")
        recertia_root = Path(args.recertia_root)
        if recertia_root.is_dir() and any(recertia_root.iterdir()):
            files = sum(1 for p in recertia_root.rglob("*") if p.is_file())
            print(f"recertia_snapshot=present files={files} root={recertia_root}")
        else:
            print("recertia_snapshot=absent (CI empty DB is allowed; ops soak should pass a snapshot)")
    finally:
        backend.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
