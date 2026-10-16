"""open_db durability pragmas."""

from __future__ import annotations

from pathlib import Path

from recertia.persistence.db import open_db


def _sync(path: Path, durability: str) -> str:
    conn = open_db(path, durability=durability)  # type: ignore[arg-type]
    try:
        row = conn.execute("PRAGMA synchronous").fetchone()
        assert row is not None
        # 0 OFF, 1 NORMAL, 2 FULL
        return {0: "OFF", 1: "NORMAL", 2: "FULL"}[int(row[0])]
    finally:
        conn.close()


def test_durable_normal_sets_synchronous_normal(tmp_path: Path) -> None:
    assert _sync(tmp_path / "n.db", "normal") == "NORMAL"


def test_durable_strict_sets_full(tmp_path: Path) -> None:
    assert _sync(tmp_path / "s.db", "strict") == "FULL"


def test_fast_sets_off(tmp_path: Path) -> None:
    assert _sync(tmp_path / "f.db", "fast") == "OFF"
