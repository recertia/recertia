"""SEC-3: legacy run_events migrate once and stay tenant-scoped."""

from __future__ import annotations

import json
from pathlib import Path

from recertia.persistence.migrate_run_events import migrate_run_events


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_migrate_run_events_moves_single_owner_and_is_idempotent(tmp_path: Path) -> None:
    legacy = tmp_path / "run_events" / "run-a.jsonl"
    _write(legacy, '{"event_id":"1"}\n')
    _write(tmp_path / "runs" / "acme" / "run-a" / "workdir.json", "{}")
    first = migrate_run_events(tmp_path)
    dest = tmp_path / "runs" / "acme" / "run_events" / "run-a.jsonl"
    assert first.moved == ("run-a",)
    assert first.pending == 0
    assert dest.read_text(encoding="utf-8") == '{"event_id":"1"}\n'
    assert not legacy.exists()
    second = migrate_run_events(tmp_path)
    assert second.moved == ()
    assert second.pending == 0
    assert dest.read_text(encoding="utf-8") == '{"event_id":"1"}\n'


def test_migrate_run_events_quarantines_ambiguous_file(tmp_path: Path) -> None:
    legacy = tmp_path / "run_events" / "shared.jsonl"
    _write(legacy, '{"event_id":"1"}\n')
    _write(tmp_path / "runs" / "a" / "shared" / "workdir.json", "{}")
    _write(tmp_path / "runs" / "b" / "shared" / "workdir.json", "{}")
    report = migrate_run_events(tmp_path)
    quarantine = tmp_path / "run_events.quarantine" / "shared.jsonl"
    assert report.quarantined == ("shared",)
    assert report.moved == ()
    assert quarantine.is_file()
    assert not (tmp_path / "runs" / "a" / "run_events" / "shared.jsonl").exists()
    assert not (tmp_path / "runs" / "b" / "run_events" / "shared.jsonl").exists()
    again = migrate_run_events(tmp_path)
    assert again.quarantined == ()
    assert again.pending == 0


def test_migrate_run_events_uses_checkpoint_row(tmp_path: Path) -> None:
    from recertia.persistence.db import open_db

    db = tmp_path / "runs" / "beta" / "checkpoints.db"
    conn = open_db(db)
    conn.execute(
        """
        CREATE TABLE checkpoints (
            run_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            node TEXT NOT NULL,
            next_node TEXT,
            state_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (run_id, seq)
        )
        """
    )
    conn.execute(
        "INSERT INTO checkpoints VALUES (?, 1, 'intake', NULL, '{}', 't')",
        ("from-db",),
    )
    conn.commit()
    conn.close()
    _write(tmp_path / "run_events" / "from-db.jsonl", json.dumps({"event_id": "e"}) + "\n")
    report = migrate_run_events(tmp_path)
    assert report.moved == ("from-db",)
    assert (tmp_path / "runs" / "beta" / "run_events" / "from-db.jsonl").is_file()


def test_identical_destination_is_not_duplicated(tmp_path: Path) -> None:
    body = '{"event_id":"1"}\n'
    _write(tmp_path / "run_events" / "run-a.jsonl", body)
    _write(tmp_path / "runs" / "acme" / "run-a" / "workdir.json", "{}")
    _write(tmp_path / "runs" / "acme" / "run_events" / "run-a.jsonl", body)
    report = migrate_run_events(tmp_path)
    assert report.pending == 0
    dest = tmp_path / "runs" / "acme" / "run_events" / "run-a.jsonl"
    assert dest.read_text(encoding="utf-8") == body
    assert not (tmp_path / "run_events").exists()
