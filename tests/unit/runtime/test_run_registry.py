"""F9: durable run_id uniqueness via RunRegistry."""

from __future__ import annotations

from pathlib import Path

import pytest

from recertia.persistence.layout import RuntimeLayout
from recertia.runtime.errors import RunIdExists
from recertia.runtime.registry import RunRegistry


def test_reserve_twice_raises_run_id_exists(tmp_path: Path) -> None:
    db = RuntimeLayout(tmp_path).tenant("demo").runtime_db
    registry = RunRegistry(db)
    try:
        registry.reserve(
            "demo",
            "run-1",
            request_hash="abc",
            summary_json={"run_id": "run-1"},
            workdir=tmp_path / "ws",
        )
        with pytest.raises(RunIdExists) as exc_info:
            registry.reserve(
                "demo",
                "run-1",
                request_hash="def",
                summary_json={"run_id": "run-1"},
            )
        assert exc_info.value.code == "run_id_exists"
        assert exc_info.value.http_status == 409
    finally:
        registry.close()


def test_reserve_survives_new_registry_instance_on_same_db(tmp_path: Path) -> None:
    db = RuntimeLayout(tmp_path).tenant("demo").runtime_db
    first = RunRegistry(db)
    try:
        first.reserve("demo", "run-persist", request_hash="h1", summary_json="{}")
    finally:
        first.close()

    second = RunRegistry(db)
    try:
        with pytest.raises(RunIdExists):
            second.reserve("demo", "run-persist", request_hash="h2", summary_json="{}")
        row = second.get("demo", "run-persist")
        assert row is not None
        assert row["request_hash"] == "h1"
        assert row["status"] == "accepted"
    finally:
        second.close()


def test_same_run_id_different_tenants_allowed(tmp_path: Path) -> None:
    layout = RuntimeLayout(tmp_path)
    a = RunRegistry(layout.tenant("tenant-a").runtime_db)
    b = RunRegistry(layout.tenant("tenant-b").runtime_db)
    try:
        a.reserve("tenant-a", "shared", request_hash="a", summary_json="{}")
        b.reserve("tenant-b", "shared", request_hash="b", summary_json="{}")
    finally:
        a.close()
        b.close()
